from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sherlock_api.api.v1.schemas.tasks import TaskOut
from sherlock_api.config import Settings, get_settings
from sherlock_api.db.enums import TaskStatus
from sherlock_api.db.models import Task
from sherlock_api.db.notify import get_notify_hub
from sherlock_api.db.session import async_session_factory
from sherlock_api.logging import get_logger

log = get_logger(__name__)

_TERMINAL = {
    TaskStatus.completed,
    TaskStatus.failed,
    TaskStatus.cancelled,
    TaskStatus.timeout,
}

_MAX_ROWS_PER_CYCLE = 64


class WebhookSweeper:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or async_session_factory()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.webhook_timeout_seconds),
            follow_redirects=False,
        )
        self._task = asyncio.create_task(self._loop(), name="webhook-sweeper")
        log.info(
            "webhook.sweeper_started",
            signed=bool(self._settings.webhook_signing_secret),
        )

    async def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._task
        self._task = None
        if t is not None:
            try:
                await asyncio.wait_for(t, timeout=timeout)
            except asyncio.TimeoutError:
                t.cancel()
                with contextlib.suppress(Exception):
                    await t
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        log.info("webhook.sweeper_stopped")

    async def _loop(self) -> None:
        interval = self._settings.webhook_sweep_interval_seconds
        hub = get_notify_hub()

        try:
            async with hub.subscribe_all() as wake:
                while not self._stop.is_set():
                    try:
                        delivered = await self._sweep_once()
                        if delivered:
                            log.info(
                                "webhook.sweep_cycle",
                                delivered=delivered,
                            )
                    except Exception:
                        log.exception("webhook.sweep_failed")

                    wake.clear()
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook.loop_crashed")

    async def _due_task_ids(self, s: AsyncSession, *, now: datetime, limit: int) -> list:
        q = (
            select(Task.id)
            .where(
                and_(
                    Task.webhook_url.isnot(None),
                    Task.webhook_delivered_at.is_(None),
                    Task.status.in_(list(_TERMINAL)),
                    Task.webhook_attempts < self._settings.webhook_max_attempts,
                    or_(
                        Task.webhook_next_attempt_at.is_(None),
                        Task.webhook_next_attempt_at <= now,
                    ),
                )
            )
            .order_by(Task.webhook_next_attempt_at.asc().nulls_first())
            .limit(limit)
        )
        rows = (await s.execute(q)).scalars().all()
        return list(rows)

    async def _sweep_once(self) -> int:
        now = datetime.now(timezone.utc)

        async with self._session_factory() as s:
            ids = await self._due_task_ids(s, now=now, limit=_MAX_ROWS_PER_CYCLE)
        if not ids:
            return 0

        ok_count = 0
        for tid in ids:
            if self._stop.is_set():
                break
            if await self._process_one(tid):
                ok_count += 1
        return ok_count

    async def _process_one(self, task_id) -> bool:
        async with self._session_factory() as s:
            locked = await s.execute(
                select(Task).where(Task.id == task_id).with_for_update(skip_locked=True)
            )
            task = locked.scalar_one_or_none()
            if task is None:
                return False

            if (
                task.webhook_url is None
                or task.webhook_delivered_at is not None
                or task.status not in _TERMINAL
                or task.webhook_attempts >= self._settings.webhook_max_attempts
            ):
                return False

            url = task.webhook_url
            attempt_no = task.webhook_attempts + 1
            body_bytes, headers = self._build_request(task, attempt_no)

        ok, err, status_code, permanent = await self._send(
            url=url,
            body_bytes=body_bytes,
            headers=headers,
            task_id=str(task_id),
            attempt_no=attempt_no,
        )

        async with self._session_factory() as s:
            fresh = await s.get(Task, task_id, with_for_update=True)
            if fresh is None:
                return False
            fresh.webhook_attempts = fresh.webhook_attempts + 1
            if ok:
                fresh.webhook_delivered_at = datetime.now(timezone.utc)
                fresh.webhook_error = None
                fresh.webhook_next_attempt_at = None
            else:
                fresh.webhook_error = (err or "")[:500] if err else None
                if permanent or fresh.webhook_attempts >= self._settings.webhook_max_attempts:
                    fresh.webhook_attempts = self._settings.webhook_max_attempts
                    fresh.webhook_next_attempt_at = None
                else:
                    fresh.webhook_next_attempt_at = datetime.now(
                        timezone.utc
                    ) + self._backoff_delay(fresh.webhook_attempts)
            await s.commit()

        return ok

    def _build_request(self, task: Task, attempt_no: int) -> tuple[bytes, dict[str, str]]:
        task_obj = TaskOut.model_validate(task).model_dump(mode="json")
        task_obj["webhook_attempts"] = attempt_no
        body_obj = {
            "event": "task.terminal",
            "event_version": 1,
            "attempt": attempt_no,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "task": task_obj,
        }
        body_bytes = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "sherlock-api-webhook/1",
            "X-Webhook-Id": f"{task.id}:{attempt_no}",
            "X-Webhook-Event": "task.terminal",
            "X-Webhook-Attempt": str(attempt_no),
            "X-Webhook-Timestamp": str(int(time.time())),
        }
        secret = self._settings.webhook_signing_secret
        if secret:
            sig_payload = headers["X-Webhook-Timestamp"].encode("ascii") + b"." + body_bytes
            digest = hmac.new(
                secret.encode("utf-8"),
                sig_payload,
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={digest}"
        return body_bytes, headers

    async def _send(
        self,
        *,
        url: str,
        body_bytes: bytes,
        headers: dict[str, str],
        task_id: str,
        attempt_no: int,
    ) -> tuple[bool, str | None, int | None, bool]:
        assert self._client is not None, "sweeper not started"
        try:
            r = await self._client.post(url, content=body_bytes, headers=headers)
        except httpx.HTTPError as e:
            log.warning(
                "webhook.transport_error",
                task_id=task_id,
                url=url,
                attempt=attempt_no,
                err=str(e),
            )
            return False, f"transport: {e.__class__.__name__}: {e}", None, False

        if 200 <= r.status_code < 300:
            log.info(
                "webhook.delivered",
                task_id=task_id,
                url=url,
                status=r.status_code,
                attempt=attempt_no,
            )
            return True, None, r.status_code, False

        retriable = r.status_code in (408, 425, 429) or r.status_code >= 500
        err_body = (r.text or "")[:200]
        log.warning(
            "webhook.http_error",
            task_id=task_id,
            url=url,
            status=r.status_code,
            attempt=attempt_no,
            retriable=retriable,
            body=err_body,
        )
        return (
            False,
            f"http {r.status_code}: {err_body}",
            r.status_code,
            not retriable,
        )

    def _backoff_delay(self, attempts: int) -> timedelta:
        base = self._settings.webhook_backoff_base_seconds
        cap = self._settings.webhook_backoff_max_seconds
        raw = min(cap, base * (2 ** max(0, attempts - 1)))
        jitter = raw * random.uniform(-0.2, 0.2)
        return timedelta(seconds=max(1.0, raw + jitter))


__all__ = ["WebhookSweeper"]

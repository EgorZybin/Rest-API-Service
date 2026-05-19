from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sherlock_api.config import Settings
from sherlock_api.db.enums import AccountStatus, TaskStatus
from sherlock_api.db.models import Account, Task
from sherlock_api.db.notify import CHANNEL as NOTIFY_CHANNEL
from sherlock_api.dispatcher.handlers import (
    HANDLERS,
    HandlerContext,
    HandlerError,
    HandlerPermanentError,
    HandlerRateLimitError,
    HandlerSubscriptionError,
)
from sherlock_api.dispatcher.handlers._audit import BufferingAuditSink, flush_audit
from sherlock_api.dispatcher.queue import TaskQueue
from sherlock_api.logging import get_logger
from sherlock_api.tg.client_factory import build_client

if TYPE_CHECKING:
    from telethon import TelegramClient


log = get_logger(__name__)

IDLE_POLL_INTERVAL = 2.0
RATE_LIMIT_BACKOFF = 60.0
TRANSIENT_RETRY_DELAY = 5.0

_NO_TG_SCENARIOS = {"noop"}


class AccountWorker:
    def __init__(
        self,
        *,
        account_id: int,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        queue: TaskQueue | None = None,
        supported_scenarios: list[str] | None = None,
    ) -> None:
        self.account_id = account_id
        self._session_factory = session_factory
        self._settings = settings
        self._queue = queue or TaskQueue()
        self._supported = supported_scenarios

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._client: TelegramClient | None = None
        self._client_needs_connect = True
        self._idle_restore_status = AccountStatus.idle

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"worker-acc-{self.account_id}")

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        t = self._task
        if t is None:
            return
        try:
            await asyncio.wait_for(t, timeout=timeout)
        except asyncio.TimeoutError:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            await self._teardown_client()

    async def _run(self) -> None:
        log.info("worker.start", account_id=self.account_id)
        cancelled = False
        try:
            while not self._stop.is_set():
                handled = await self._tick()
                if not handled:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=IDLE_POLL_INTERVAL)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            cancelled = True
            log.info("worker.cancelled", account_id=self.account_id)
        except Exception:
            log.exception("worker.crashed", account_id=self.account_id)
        finally:
            try:
                await self._sanitize_state()
            except Exception:
                log.exception("worker.sanitize_failed", account_id=self.account_id)
            log.info("worker.stop", account_id=self.account_id)
        if cancelled:
            raise asyncio.CancelledError()

    async def _sanitize_state(self) -> None:
        """Roll back any in-flight rows owned by this worker.

        Runs in worker `_run`'s finally. Uses a fresh session so it survives
        even if the worker's previous session was poisoned by the same fault
        that caused the exit. Best-effort: any failure is logged by the caller
        and the periodic sanitiser in DispatcherManager will pick up the slack.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            try:
                await session.execute(
                    update(Task)
                    .where(Task.account_id == self.account_id)
                    .where(Task.status == TaskStatus.running)
                    .values(
                        status=TaskStatus.pending,
                        account_id=None,
                        started_at=None,
                        error_code="worker_exited",
                        error_message=(
                            f"worker for account {self.account_id} exited "
                            f"without finishing task at {now.isoformat()}"
                        ),
                    )
                )
                await session.execute(
                    update(Account)
                    .where(Account.id == self.account_id)
                    .where(Account.status == AccountStatus.busy)
                    .values(
                        status=AccountStatus.idle,
                        status_reason="worker exited; reverted by worker self-sanitise",
                    )
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _tick(self) -> bool:
        async with self._session_factory() as session:
            account = await self._load_account(session)
            if account is None:
                await session.commit()
                return False

            if account.status not in {
                AccountStatus.idle,
                AccountStatus.subscription_expired,
            }:
                return False

            wait = self._cooldown_remaining(account)
            if wait > 0:
                await session.commit()
                await self._sleep_interruptible(wait)
                return True

            if (
                account.status != AccountStatus.subscription_expired
                and self._hour_cap_reached(account)
            ):
                resume_in = self._seconds_to_next_hour(account)
                await session.commit()
                log.debug(
                    "worker.hour_cap",
                    account_id=self.account_id,
                    resume_in=resume_in,
                )
                await self._sleep_interruptible(min(resume_in, 60.0))
                return True

            task = await self._queue.pick_for_account(
                session,
                account_id=self.account_id,
                supported_scenarios=self._supported,
                account_status=account.status,
            )
            if task is None:
                await session.commit()
                return False

            self._idle_restore_status = account.status
            account.status = AccountStatus.busy
            account.status_reason = f"running task {task.id}"
            await session.commit()

        await self._execute_task(task.id)
        return True

    async def _execute_task(self, task_id: Any) -> None:
        async with self._session_factory() as session:
            account = await self._load_account(session)
            task = await session.get(Task, task_id)
            if account is None or task is None:
                return
            handler = HANDLERS.get(task.scenario)
            if handler is None:
                await self._queue.fail_final(
                    session,
                    task.id,
                    error_code="unknown_scenario",
                    error_message=f"no handler registered for {task.scenario!r}",
                )
                await self._release_account(session, account, idle=True)
                await session.commit()
                return

            needs_tg = task.scenario not in _NO_TG_SCENARIOS
            client = None
            if needs_tg:
                try:
                    client = await self._get_client(account)
                except Exception as e:
                    log.warning(
                        "worker.client_failed",
                        account_id=self.account_id,
                        err=repr(e),
                    )
                    await self._queue.requeue(
                        session,
                        task.id,
                        error_code="client_unavailable",
                        error_message=f"{type(e).__name__}: {e}",
                    )
                    await self._release_account(
                        session,
                        account,
                        idle=False,
                        status=AccountStatus.dead,
                        reason=f"client unavailable: {type(e).__name__}",
                    )
                    await session.commit()
                    return

            audit_sink = BufferingAuditSink()
            ctx = HandlerContext(
                client=client,
                account=account,
                task=task,
                bot_username=self._settings.sherlock_bot_username,
                audit=audit_sink,
                session=session,
            )

            try:
                outcome = await asyncio.wait_for(
                    handler(ctx),
                    timeout=float(self._settings.account_response_timeout) * 2,
                )
                await self._queue.complete(session, task.id, result=outcome.data)
                self._note_request(account)
                await self._release_account(session, account, idle=True)
            except HandlerSubscriptionError as e:
                await self._queue.requeue(
                    session,
                    task.id,
                    error_code=e.code,
                    error_message=str(e),
                )
                await self._release_account(
                    session,
                    account,
                    idle=False,
                    status=AccountStatus.subscription_expired,
                    reason=str(e),
                )
            except HandlerPermanentError as e:
                await self._queue.fail_final(
                    session,
                    task.id,
                    error_code=e.code,
                    error_message=str(e),
                )
                await self._release_account(session, account, idle=True)
            except HandlerRateLimitError as e:
                await self._queue.requeue(
                    session,
                    task.id,
                    error_code=e.code,
                    error_message=str(e),
                )
                self._note_request(account)
                account.last_request_at = datetime.now(timezone.utc) + timedelta(
                    seconds=RATE_LIMIT_BACKOFF - self._settings.account_cooldown_seconds
                )
                await self._release_account(session, account, idle=True)
            except asyncio.TimeoutError:
                pages_seen = sum(1 for ev in audit_sink.events if ev.kind == "result_page")
                retryable = (task.attempts or 0) < (task.max_attempts or 1)
                if retryable and pages_seen == 0:
                    await self._queue.requeue(
                        session,
                        task.id,
                        error_code="handler_timeout",
                        error_message=(
                            f"handler exceeded {self._settings.account_response_timeout * 2}s"
                        ),
                    )
                else:
                    await self._queue.fail_final(
                        session,
                        task.id,
                        error_code="handler_timeout",
                        error_message=(
                            "timeout with partial progress; retries disabled to avoid "
                            "duplicate bot requests"
                            if pages_seen > 0
                            else "exhausted retries on timeout"
                        ),
                        status=TaskStatus.timeout,
                    )
                self._note_request(account)
                await self._release_account(session, account, idle=True)
            except HandlerError as e:
                pages_seen = sum(1 for ev in audit_sink.events if ev.kind == "result_page")
                retryable = (task.attempts or 0) < (task.max_attempts or 1)
                if retryable and pages_seen == 0:
                    await self._queue.requeue(
                        session,
                        task.id,
                        error_code=e.code,
                        error_message=str(e),
                    )
                else:
                    await self._queue.fail_final(
                        session,
                        task.id,
                        error_code=e.code,
                        error_message=(
                            f"{e} (partial progress detected; retries disabled)"
                            if pages_seen > 0
                            else str(e)
                        ),
                    )
                self._note_request(account)
                await self._release_account(session, account, idle=True)
            except Exception as e:
                log.exception(
                    "worker.handler_crash",
                    account_id=self.account_id,
                    task_id=str(task.id),
                )
                retryable = (task.attempts or 0) < (task.max_attempts or 1)
                if retryable:
                    await self._queue.requeue(
                        session,
                        task.id,
                        error_code="handler_crash",
                        error_message=f"{type(e).__name__}: {e}",
                    )
                else:
                    await self._queue.fail_final(
                        session,
                        task.id,
                        error_code="handler_crash",
                        error_message=f"{type(e).__name__}: {e}",
                    )
                self._note_request(account)
                await self._release_account(session, account, idle=True)

            try:
                written = await flush_audit(
                    session,
                    audit_sink,
                    task_id=task.id,
                    account_id=self.account_id,
                )
                if written:
                    log.debug(
                        "worker.audit_flushed",
                        task_id=str(task.id),
                        events=written,
                    )
            except Exception:
                log.exception("worker.audit_flush_failed", task_id=str(task.id))

            await self._emit_task_done(session, task)

            await session.commit()

    async def _emit_task_done(self, session: AsyncSession, task: Task) -> None:
        terminal = {
            TaskStatus.completed,
            TaskStatus.failed,
            TaskStatus.cancelled,
            TaskStatus.timeout,
        }
        if task.status not in terminal:
            return
        try:
            await session.execute(
                text("SELECT pg_notify(:ch, :payload)"),
                {"ch": NOTIFY_CHANNEL, "payload": str(task.id)},
            )
        except Exception:
            log.warning(
                "worker.notify_failed",
                task_id=str(task.id),
                exc_info=True,
            )

    async def _load_account(self, session: AsyncSession) -> Account | None:
        res = await session.execute(select(Account).where(Account.id == self.account_id))
        return res.scalar_one_or_none()

    async def _release_account(
        self,
        session: AsyncSession,
        account: Account,
        *,
        idle: bool,
        status: AccountStatus | None = None,
        reason: str | None = None,
    ) -> None:
        if idle:
            account.status = status or self._idle_restore_status
            account.status_reason = reason or "idle"
        else:
            account.status = status or AccountStatus.paused
            account.status_reason = reason or account.status_reason

    def _cooldown_remaining(self, account: Account) -> float:
        if not account.last_request_at:
            return 0.0
        elapsed = (datetime.now(timezone.utc) - _aware(account.last_request_at)).total_seconds()
        return max(0.0, self._settings.account_cooldown_seconds - elapsed)

    def _hour_cap_reached(self, account: Account) -> bool:
        if not account.hour_window_started_at:
            return False
        now = datetime.now(timezone.utc)
        if now - _aware(account.hour_window_started_at) >= timedelta(hours=1):
            return False
        return (account.requests_last_hour or 0) >= (self._settings.account_requests_per_hour)

    def _seconds_to_next_hour(self, account: Account) -> float:
        if not account.hour_window_started_at:
            return 0.0
        reset_at = _aware(account.hour_window_started_at) + timedelta(hours=1)
        return max(0.0, (reset_at - datetime.now(timezone.utc)).total_seconds())

    def _note_request(self, account: Account) -> None:
        now = datetime.now(timezone.utc)
        account.last_request_at = now
        account.requests_total = (account.requests_total or 0) + 1

        start = account.hour_window_started_at
        if start is None or now - _aware(start) >= timedelta(hours=1):
            account.hour_window_started_at = now
            account.requests_last_hour = 1
        else:
            account.requests_last_hour = (account.requests_last_hour or 0) + 1

    async def _sleep_interruptible(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _get_client(self, account: Account) -> "TelegramClient":
        if self._client is not None and not self._client_needs_connect:
            if self._client.is_connected():
                return self._client
            self._client_needs_connect = True

        if self._client is None:
            self._client = build_client(account)

        try:
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError("session not authorized")
        except Exception:
            await self._teardown_client()
            raise

        self._client_needs_connect = False
        return self._client

    async def _teardown_client(self) -> None:
        c = self._client
        self._client = None
        self._client_needs_connect = True
        if c is None:
            return
        try:
            await c.disconnect()
        except Exception:
            pass


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

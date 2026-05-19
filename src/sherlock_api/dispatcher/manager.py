from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sherlock_api.config import Settings, get_settings
from sherlock_api.db.enums import AccountStatus, TaskStatus
from sherlock_api.db.models import Account, Task
from sherlock_api.db.notify import get_notify_hub
from sherlock_api.db.session import async_session_factory
from sherlock_api.dispatcher.queue import TaskQueue
from sherlock_api.dispatcher.webhook import WebhookSweeper
from sherlock_api.dispatcher.worker import AccountWorker
from sherlock_api.logging import get_logger

log = get_logger(__name__)

_LIVE_STATUSES = {
    AccountStatus.idle,
    AccountStatus.busy,
    AccountStatus.subscription_expired,
}
RECONCILE_INTERVAL = 15.0
SANITISER_INTERVAL = 300.0
SUPERVISOR_BACKOFF_MAX = 30.0


class DispatcherManager:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        supported_scenarios: Iterable[str] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or async_session_factory()
        self._supported = list(supported_scenarios) if supported_scenarios else None
        self._workers: dict[int, AccountWorker] = {}
        self._queue = TaskQueue()
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._sanitiser_task: asyncio.Task[None] | None = None
        self._webhook_sweeper = WebhookSweeper(
            settings=self._settings,
            session_factory=self._session_factory,
        )

    async def start(self) -> None:
        try:
            await get_notify_hub().start()
        except Exception:
            log.exception("dispatcher.notify_hub_start_failed")

        await self._reset_inflight()
        await self._reconcile_once()
        self._stop.clear()
        self._loop_task = asyncio.create_task(
            self._supervise(self._reconcile_loop, "reconcile"),
            name="dispatcher-supervise-reconcile",
        )
        self._sanitiser_task = asyncio.create_task(
            self._supervise(self._sanitiser_loop, "sanitiser"),
            name="dispatcher-supervise-sanitiser",
        )
        await self._webhook_sweeper.start()
        log.info("dispatcher.started", workers=len(self._workers))

    async def stop(self, *, worker_timeout: float = 10.0) -> None:
        self._stop.set()
        for t in (self._loop_task, self._sanitiser_task):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=3.0)
            except asyncio.TimeoutError:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        if self._workers:
            await asyncio.gather(
                *(w.stop(timeout=worker_timeout) for w in self._workers.values()),
                return_exceptions=True,
            )
        await self._webhook_sweeper.stop()
        log.info("dispatcher.stopped")

    async def wait_forever(self) -> None:
        await self._stop.wait()

    async def _reset_inflight(self) -> None:
        async with self._session_factory() as session:
            reset = await self._queue.reset_inflight(session)
            await session.execute(
                Account.__table__.update()
                .where(Account.status == AccountStatus.busy)
                .values(
                    status=AccountStatus.idle,
                    status_reason="reset on dispatcher startup",
                )
            )
            await session.commit()
            if reset:
                log.warning("dispatcher.reset_inflight", tasks=reset)

    async def _supervise(self, body, name: str) -> None:
        """Run body() forever, restarting it on any non-shutdown exit."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await body()
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                log.warning("dispatcher.supervisor_external_cancel", loop=name)
            except Exception:
                log.exception("dispatcher.supervisor_crashed", loop=name)
            if self._stop.is_set():
                break
            log.warning("dispatcher.supervisor_restart", loop=name, backoff=backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, SUPERVISOR_BACKOFF_MAX)

    async def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                log.warning("dispatcher.reconcile_external_cancel")
                continue
            except Exception:
                log.exception("dispatcher.reconcile_failed")

    async def _sanitiser_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SANITISER_INTERVAL)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self._sanitise_once()
            except asyncio.CancelledError:
                if self._stop.is_set():
                    raise
                log.warning("dispatcher.sanitiser_external_cancel")
                continue
            except Exception:
                log.exception("dispatcher.sanitiser_failed")

    async def _reconcile_once(self) -> None:
        async with self._session_factory() as session:
            live_ids = await self._live_account_ids(session)

        for acc_id in live_ids:
            if acc_id not in self._workers:
                w = AccountWorker(
                    account_id=acc_id,
                    session_factory=self._session_factory,
                    settings=self._settings,
                    queue=self._queue,
                    supported_scenarios=self._supported,
                )
                w.start()
                self._workers[acc_id] = w
                log.info("dispatcher.worker_spawned", account_id=acc_id)

        gone = [acc_id for acc_id in self._workers if acc_id not in live_ids]
        for acc_id in gone:
            w = self._workers.pop(acc_id)
            await w.stop()
            log.info("dispatcher.worker_removed", account_id=acc_id)

    async def _sanitise_once(self) -> None:
        """Sweep orphan in-flight rows that no live worker is going to clean up.

        Two cleanup classes:
          1. tasks.status == 'running' whose account_id has no live worker
             OR whose started_at is older than 4 × account_response_timeout
             (handler_timeout is 2 × that; double again as safety margin).
          2. accounts.status == 'busy' whose id has no live worker in memory.

        Both indicate the owning worker died without running its finally cleanup
        (e.g. process killed mid-task, BaseException cascade).
        """
        live_worker_ids = set(self._workers.keys())
        now = datetime.now(timezone.utc)
        stale_after = now - timedelta(
            seconds=self._settings.account_response_timeout * 4
        )

        async with self._session_factory() as session:
            running = (
                await session.execute(
                    select(Task).where(Task.status == TaskStatus.running)
                )
            ).scalars().all()

            stale_tasks: list[Task] = []
            for t in running:
                no_live_worker = (
                    t.account_id is None or t.account_id not in live_worker_ids
                )
                started = t.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                too_old = started is not None and started < stale_after
                if no_live_worker or too_old:
                    stale_tasks.append(t)

            for t in stale_tasks:
                t.status = TaskStatus.pending
                t.account_id = None
                t.started_at = None
                t.error_code = "stale_inflight"
                t.error_message = (
                    f"sanitiser reset at {now.isoformat()} "
                    f"(no_live_worker={t.account_id is None or t.account_id not in live_worker_ids}, "
                    f"too_old={too_old})"
                )

            orphan_q = (
                update(Account)
                .where(Account.status == AccountStatus.busy)
                .values(
                    status=AccountStatus.idle,
                    status_reason="sanitiser: no live worker",
                )
            )
            if live_worker_ids:
                orphan_q = orphan_q.where(~Account.id.in_(live_worker_ids))
            orphan_res = await session.execute(orphan_q)

            await session.commit()

            stale_count = len(stale_tasks)
            orphan_count = orphan_res.rowcount or 0
            if stale_count or orphan_count:
                log.warning(
                    "dispatcher.sanitised",
                    stale_tasks=stale_count,
                    orphan_accounts=orphan_count,
                    live_workers=len(live_worker_ids),
                )

    async def _live_account_ids(self, session: AsyncSession) -> set[int]:
        q = select(Account.id).where(Account.status.in_(_LIVE_STATUSES))
        rows = (await session.execute(q)).all()
        return {r[0] for r in rows}

    @property
    def worker_count(self) -> int:
        return len(self._workers)

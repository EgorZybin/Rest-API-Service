from __future__ import annotations

import asyncio
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sherlock_api.config import Settings, get_settings
from sherlock_api.db.enums import AccountStatus
from sherlock_api.db.models import Account
from sherlock_api.db.notify import get_notify_hub
from sherlock_api.db.session import async_session_factory
from sherlock_api.dispatcher.queue import TaskQueue
from sherlock_api.dispatcher.webhook import WebhookSweeper
from sherlock_api.dispatcher.worker import AccountWorker
from sherlock_api.logging import get_logger

log = get_logger(__name__)

_LIVE_STATUSES = {AccountStatus.idle, AccountStatus.busy}
RECONCILE_INTERVAL = 15.0


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
        self._loop_task = asyncio.create_task(self._reconcile_loop(), name="dispatcher-reconcile")
        await self._webhook_sweeper.start()
        log.info("dispatcher.started", workers=len(self._workers))

    async def stop(self, *, worker_timeout: float = 10.0) -> None:
        self._stop.set()
        t = self._loop_task
        if t is not None:
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

    async def _reconcile_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                try:
                    await self._reconcile_once()
                except Exception:
                    log.exception("dispatcher.reconcile_failed")
        except asyncio.CancelledError:
            raise

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

    async def _live_account_ids(self, session: AsyncSession) -> set[int]:
        q = select(Account.id).where(Account.status.in_(_LIVE_STATUSES))
        rows = (await session.execute(q)).all()
        return {r[0] for r in rows}

    @property
    def worker_count(self) -> int:
        return len(self._workers)

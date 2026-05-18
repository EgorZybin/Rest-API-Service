from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import asyncpg

from sherlock_api.config import get_settings
from sherlock_api.logging import get_logger

log = get_logger(__name__)

CHANNEL = "sherlock_task_done"

_BACKOFF_MAX = 30.0
_INITIAL_CONNECT_WAIT = 10.0


@dataclass(eq=False)
class _Subscription:
    task_id: uuid.UUID
    event: asyncio.Event | None = None
    queue: asyncio.Queue[str] | None = None

    def deliver(self, payload: str) -> None:
        if self.event is not None and not self.event.is_set():
            self.event.set()
        if self.queue is not None:
            try:
                self.queue.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("notify.queue_full", task_id=str(self.task_id))


class NotifyHub:
    """Postgres LISTEN hub with reconnecting supervisor.

    The supervisor task runs for the hub's lifetime, holding a single asyncpg
    connection with the LISTEN registered. On any termination (network blip,
    PG restart, idle TCP drop), it logs and reconnects with exponential
    backoff. Subscriptions registered via `wait_once`/`subscribe_all`/`stream`
    survive reconnects — only events fired strictly during the downtime are
    lost (LISTEN/NOTIFY has no replay).
    """

    def __init__(self) -> None:
        self._conn: asyncpg.Connection | None = None
        self._subs: dict[uuid.UUID, set[_Subscription]] = {}
        self._broadcast: set[asyncio.Event] = set()
        self._lock = asyncio.Lock()
        self._started = False
        self._stop_evt = asyncio.Event()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._conn_ready = asyncio.Event()

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_evt.clear()
            self._conn_ready.clear()
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="notify-supervisor"
            )
        try:
            await asyncio.wait_for(self._conn_ready.wait(), timeout=_INITIAL_CONNECT_WAIT)
            log.info("notify.hub_started", channel=CHANNEL)
        except asyncio.TimeoutError:
            log.warning(
                "notify.start_timeout",
                channel=CHANNEL,
                wait=_INITIAL_CONNECT_WAIT,
            )

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop_evt.set()
            t = self._supervisor_task
            self._supervisor_task = None

        if t is not None:
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except asyncio.TimeoutError:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            except (asyncio.CancelledError, Exception):
                pass

        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        for subs in self._subs.values():
            for s in subs:
                s.deliver("<shutdown>")
        self._subs.clear()
        for evt in self._broadcast:
            evt.set()
        self._broadcast.clear()
        log.info("notify.hub_stopped")

    async def _supervise(self) -> None:
        backoff = 1.0
        attempt = 0
        while not self._stop_evt.is_set():
            term_evt = asyncio.Event()
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(dsn=_dsn_for_asyncpg())
                conn.add_termination_listener(lambda _c: term_evt.set())
                await conn.add_listener(CHANNEL, self._on_notify)
                self._conn = conn
                self._conn_ready.set()
                if attempt > 0:
                    log.warning("notify.reconnected", attempt=attempt, channel=CHANNEL)
                attempt = 0
                backoff = 1.0

                stop_wait = asyncio.create_task(self._stop_evt.wait())
                term_wait = asyncio.create_task(term_evt.wait())
                try:
                    done, pending = await asyncio.wait(
                        {stop_wait, term_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for t in (stop_wait, term_wait):
                        if not t.done():
                            t.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await t

                self._conn_ready.clear()
                self._conn = None
                with contextlib.suppress(Exception):
                    await conn.close()

                if self._stop_evt.is_set():
                    return
                log.warning("notify.connection_terminated", channel=CHANNEL)
            except asyncio.CancelledError:
                self._conn_ready.clear()
                self._conn = None
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                if self._stop_evt.is_set():
                    return
                log.warning("notify.supervisor_external_cancel")
            except Exception:
                self._conn_ready.clear()
                self._conn = None
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                log.exception(
                    "notify.connect_failed", attempt=attempt + 1, channel=CHANNEL
                )

            attempt += 1
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _on_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        for evt in list(self._broadcast):
            if not evt.is_set():
                evt.set()
        try:
            tid = uuid.UUID(payload)
        except Exception:
            log.warning("notify.bad_payload", payload=payload[:80])
            return
        subs = self._subs.get(tid)
        if not subs:
            return
        for s in list(subs):
            s.deliver(payload)

    def _ensure_bucket(self, task_id: uuid.UUID) -> set[_Subscription]:
        bucket = self._subs.get(task_id)
        if bucket is None:
            bucket = set()
            self._subs[task_id] = bucket
        return bucket

    def _drop_sub(self, sub: _Subscription) -> None:
        bucket = self._subs.get(sub.task_id)
        if bucket is None:
            return
        bucket.discard(sub)
        if not bucket:
            self._subs.pop(sub.task_id, None)

    @contextlib.asynccontextmanager
    async def wait_once(self, task_id: uuid.UUID) -> AsyncIterator[asyncio.Event]:
        if not self._started:
            await self.start()
        evt = asyncio.Event()
        sub = _Subscription(task_id=task_id, event=evt)
        self._ensure_bucket(task_id).add(sub)
        try:
            yield evt
        finally:
            self._drop_sub(sub)

    @contextlib.asynccontextmanager
    async def subscribe_all(self) -> AsyncIterator[asyncio.Event]:
        if not self._started:
            await self.start()
        evt = asyncio.Event()
        self._broadcast.add(evt)
        try:
            yield evt
        finally:
            self._broadcast.discard(evt)

    @contextlib.asynccontextmanager
    async def stream(
        self, task_id: uuid.UUID, *, max_queue: int = 64
    ) -> AsyncIterator[asyncio.Queue[str]]:
        if not self._started:
            await self.start()
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        sub = _Subscription(task_id=task_id, queue=q)
        self._ensure_bucket(task_id).add(sub)
        try:
            yield q
        finally:
            self._drop_sub(sub)


_hub: NotifyHub | None = None


def get_notify_hub() -> NotifyHub:
    global _hub
    if _hub is None:
        _hub = NotifyHub()
    return _hub


async def shutdown_notify_hub() -> None:
    global _hub
    if _hub is not None:
        await _hub.stop()
        _hub = None


def _dsn_for_asyncpg() -> str:
    url = get_settings().database_url
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    return url


__all__ = [
    "CHANNEL",
    "NotifyHub",
    "get_notify_hub",
    "shutdown_notify_hub",
]

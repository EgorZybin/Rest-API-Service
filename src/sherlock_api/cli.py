from __future__ import annotations

import asyncio

import typer
import uvicorn

from sherlock_api.config import get_settings

app = typer.Typer(add_completion=False, help="Sherlock API management CLI")

db_app = typer.Typer(help="Database / migrations helpers")
tasks_app = typer.Typer(help="Inspect and manipulate the task queue")
dispatcher_app = typer.Typer(help="Run the task dispatcher")
app.add_typer(db_app, name="db")
app.add_typer(tasks_app, name="tasks")
app.add_typer(dispatcher_app, name="dispatcher")


@app.command()
def serve(reload: bool = False) -> None:
    settings = get_settings()
    uvicorn.run(
        "sherlock_api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=reload,
    )


@app.command()
def version() -> None:
    from sherlock_api import __version__

    typer.echo(__version__)


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url_sync)
    command.upgrade(cfg, revision)


@db_app.command("downgrade")
def db_downgrade(revision: str = "-1") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url_sync)
    command.downgrade(cfg, revision)


@db_app.command("current")
def db_current() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url_sync)
    command.current(cfg, verbose=True)


@tasks_app.command("list")
def tasks_list(
    status: str | None = typer.Option(None, "--status", help="Filter by TaskStatus."),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    from sqlalchemy import select

    from sherlock_api.db.models import Task
    from sherlock_api.db.session import async_session_factory, dispose_engine

    async def _run() -> None:
        factory = async_session_factory()
        try:
            async with factory() as session:
                stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
                if status:
                    stmt = stmt.where(Task.status == status)
                rows = (await session.execute(stmt)).scalars().all()
            typer.echo(f"{'CREATED':<20} {'STATUS':<10} {'SCENARIO':<14} {'ACC':<4} {'ATT':<3} ID")
            for t in rows:
                typer.echo(
                    f"{t.created_at.strftime('%Y-%m-%d %H:%M:%S'):<20} "
                    f"{t.status.value:<10} {t.scenario:<14} "
                    f"{(t.account_id or '-'):<4} {t.attempts:<3} {t.id}"
                )
        finally:
            await dispose_engine()

    asyncio.run(_run())


@tasks_app.command("show")
def tasks_show(task_id: str = typer.Argument(...)) -> None:
    import json as _json
    import uuid as _uuid

    from sherlock_api.db.models import Task
    from sherlock_api.db.session import async_session_factory, dispose_engine

    async def _run() -> None:
        factory = async_session_factory()
        try:
            async with factory() as session:
                t = await session.get(Task, _uuid.UUID(task_id))
                if t is None:
                    raise typer.Exit(code=1)
                out = {
                    "id": str(t.id),
                    "scenario": t.scenario,
                    "status": t.status.value,
                    "account_id": t.account_id,
                    "attempts": t.attempts,
                    "max_attempts": t.max_attempts,
                    "priority": t.priority,
                    "created_at": t.created_at.isoformat(),
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                    "input": t.input,
                    "result": t.result,
                    "error_code": t.error_code,
                    "error_message": t.error_message,
                }
                typer.echo(_json.dumps(out, indent=2, ensure_ascii=False))
        finally:
            await dispose_engine()

    asyncio.run(_run())


@dispatcher_app.command("run")
def dispatcher_run() -> None:
    import signal

    from sherlock_api.db.notify import shutdown_notify_hub
    from sherlock_api.db.session import dispose_engine
    from sherlock_api.dispatcher import DispatcherManager
    from sherlock_api.logging import configure_logging

    configure_logging()

    async def _run() -> None:
        mgr = DispatcherManager()
        await mgr.start()
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal() -> None:
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal)
            except NotImplementedError:
                pass

        try:
            await stop_event.wait()
        finally:
            await mgr.stop()
            await shutdown_notify_hub()
            await dispose_engine()

    asyncio.run(_run())


if __name__ == "__main__":
    app()

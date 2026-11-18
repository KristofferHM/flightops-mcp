"""`uv run python -m dashboard`: serve the board, record it, or replay a recording."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import FastAPI

from flightops_mcp.airports import AirportDB, build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.resilience import configure_breakers
from flightops_mcp.runtime import get_runtime
from flightops_mcp.server import log_opensky_mode
from flightops_mcp.telemetry import configure_logging, get_logger

from .app import create_app
from .gate import LOCAL_BIND, Gate, bind_host
from .service import BoardService
from .sources import (
    REPO_ROOT,
    LiveSource,
    RecordedSource,
    RecordingError,
    RecordingSource,
    ToolSource,
    default_directory,
)

SEED_CSV: Final = REPO_ROOT / "data" / "airports_seed.csv"
BOARD_SIZE: Final = 30

_log = get_logger(__name__)


def load_airports(settings: Settings) -> list[dict[str, Any]]:
    """The listed airports. A fresh clone builds its reference data from the seed file."""
    if not settings.airports_db.is_file():
        stored = build_from_csv(SEED_CSV, settings.airports_db)
        _log.info("dashboard_reference_data_built", airports=stored)
    return AirportDB(settings.airports_db).board(limit=BOARD_SIZE)


def build(args: argparse.Namespace, settings: Settings) -> tuple[FastAPI, str]:
    """The app and the address to bind. Raises `RecordingError` for a bad recording."""
    secret = settings.dashboard_passcode
    passcode = secret.get_secret_value() if secret else None
    host = bind_host(args.host, passcode)
    if not passcode:
        _log.warning(
            "dashboard_localhost_only",
            reason="FLIGHTOPS_DASHBOARD_PASSCODE is not set, so there is no gate",
            requested_host=args.host,
            bound_host=LOCAL_BIND,
        )

    source: ToolSource
    close = None
    recorder: RecordingSource | None = None
    if args.replay:
        source = RecordedSource(args.replay)
        _log.info("dashboard_replaying", recorded_at=source.recorded_at)
    else:
        log_opensky_mode(settings)
        configure_breakers(
            failure_threshold=settings.breaker_failure_threshold,
            recovery_timeout_s=settings.breaker_recovery_timeout_s,
        )
        source = LiveSource()
        if args.record is not None:
            chosen = Path(args.record) if args.record else settings.dashboard_recordings_dir
            recorder = RecordingSource(source, default_directory(chosen))
            source = recorder
            _log.info("dashboard_recording", file=recorder.path.name)
        kept = recorder

        async def close_live() -> None:
            await get_runtime().aclose()
            if kept is not None:
                kept.close()

        close = close_live

    service = BoardService(source, load_airports(settings))
    return create_app(service, Gate(passcode), on_close=close), host


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m dashboard")
    parser.add_argument("--host", default=LOCAL_BIND)
    parser.add_argument("--port", type=int, default=8765)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--record",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="save every tool response; outside the repository only",
    )
    mode.add_argument(
        "--replay",
        type=Path,
        metavar="FILE",
        help="serve a recording, with no upstream calls at all",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        app, host = build(args, settings)
    except (RecordingError, OSError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

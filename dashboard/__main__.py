"""`uv run python -m dashboard`: serve the board, record it, or replay a recording."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import uvicorn
from fastapi import FastAPI

from flightops_mcp.airports import WORLD_TYPES, AirportDB, build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.edition import EditionPlan, EditionRefusedError, check_edition
from flightops_mcp.providers import ProviderError
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
WORLD_CSV: Final = REPO_ROOT / "data" / "airports_world.csv"

_log = get_logger(__name__)


class StaleReferenceData(OSError):
    """A reference database from before the board drew continents."""


def load_airports(settings: Settings, csv_path: Path = WORLD_CSV) -> list[dict[str, Any]]:
    """The listed airports. A fresh clone builds its reference data from the world file.

    An existing file is never rebuilt behind the user's back: it may be a full build
    they chose. One without the continent column is refused, with the command to run.
    """
    path = settings.airports_db
    if not path.is_file():
        stored = build_from_csv(csv_path, path)
        _log.info("dashboard_reference_data_built", airports=stored, source=csv_path.name)
    database = AirportDB(path)
    if not database.has_continents():
        raise StaleReferenceData(
            f"{path} was built before the board drew continents. Rebuild it with: "
            f"uv run python scripts/build_airports_db.py --csv data/airports_world.csv "
            f"--db {path.as_posix()}"
        )
    # A full build holds tens of thousands of fields and strips. The board draws the
    # world set: airports with scheduled service, large and medium.
    return [
        airport
        for airport in database.placed()
        if airport["type"] in WORLD_TYPES and airport["scheduled_service"] == "yes"
    ]


class EditionMismatch(ValueError):
    """A request this edition must refuse at startup."""


def build(
    args: argparse.Namespace, settings: Settings, edition: EditionPlan | None = None
) -> tuple[FastAPI, str]:
    """The app and the address to bind. Raises `RecordingError` for a bad recording.

    Raises `EditionRefusedError` for a source the edition may not serve, and
    `EditionMismatch` for a replay outside the demo: a recording holds whatever the
    process that made it served, and only the demo may serve all of it.
    """
    plan = edition or check_edition(settings)
    if args.replay and plan.edition != "demo":
        raise EditionMismatch(
            f"--replay serves a recording as it was made, which may hold OpenSky data "
            f"(licence class C). The {plan.edition} edition does not replay; run "
            "FLIGHTOPS_EDITION=demo."
        )
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

    service = BoardService(
        source,
        load_airports(settings),
        traffic_ceiling_per_hour=settings.dashboard_traffic_credits_per_hour,
    )
    return create_app(service, Gate(passcode), on_close=close, edition=plan), host


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
    except (RecordingError, OSError, EditionRefusedError, EditionMismatch, ProviderError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

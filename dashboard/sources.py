"""Where the board's payloads come from: the live tools, or a recording of them.

The recording format is JSON lines, one header and then one entry per tool call:

    {"format": "flightops-dashboard-recording", "version": 1, "created_at": "..."}
    {"t": "...", "tool": "...", "arguments": {...}, "payload_sha256": "...", "payload": {...}}

Replay reads only that file. It holds no client and no runtime, so it cannot reach an
upstream, which is what makes a demo repeat exactly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, TextIO

from flightops_mcp import server as srv
from flightops_mcp.opensky import daily_metered

FORMAT: Final = "flightops-dashboard-recording"
VERSION: Final = 1
REPO_ROOT: Final = Path(__file__).resolve().parent.parent

Payload = dict[str, Any]
Tool = Callable[..., Awaitable[Payload]]

# Read tools only, named one by one. The board has no route to a passenger or a write.
LIVE_TOOLS: Final[Mapping[str, Tool]] = {
    "airport_weather": srv.airport_weather,
    "weather_overview": srv.weather_overview,
    "airport_live_traffic": srv.airport_live_traffic,
    "airspace_snapshot": srv.airspace_snapshot,
    "aircraft_track": srv.aircraft_track,
    "disruption_triage": srv.disruption_triage,
    "upstream_status": srv.upstream_status,
}


class RecordingError(ValueError):
    """A recording that cannot be used, or a place one must not be written."""


class ToolSource(Protocol):
    @property
    def recorded_at(self) -> str | None: ...

    async def call(self, tool: str, arguments: Payload) -> Payload: ...


class LiveSource:
    """The real tools. Everything they spend counts against the daily guard."""

    recorded_at: None = None

    async def call(self, tool: str, arguments: Payload) -> Payload:
        with daily_metered():
            return await LIVE_TOOLS[tool](**arguments)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(payload: Payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def default_directory(configured: Path | None) -> Path:
    return configured or Path.home() / ".flightops" / "recordings"


def outside_repository(path: Path) -> Path:
    """`path`, if it is outside this repository. Real responses do not belong in git."""
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise RecordingError(f"recordings stay outside the repository, not in {resolved}")
    return resolved


class RecordingSource:
    """Pass calls through to `inner`, and write each answer down."""

    recorded_at: None = None

    def __init__(self, inner: ToolSource, directory: Path) -> None:
        self._inner = inner
        target = outside_repository(directory)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = target / f"board-{stamp}.jsonl"
        self._file: TextIO = self.path.open("x", encoding="utf-8", newline="\n")
        self._write(
            {"format": FORMAT, "version": VERSION, "created_at": datetime.now(UTC).isoformat()}
        )

    def _write(self, record: Payload) -> None:
        self._file.write(json.dumps(record, sort_keys=True) + "\n")
        self._file.flush()

    async def call(self, tool: str, arguments: Payload) -> Payload:
        payload = await self._inner.call(tool, arguments)
        kept = dict(payload)
        # A filesystem path from this machine has no place in a file meant to be shared.
        kept.pop("reference_data", None)
        self._write(
            {
                "t": datetime.now(UTC).isoformat(),
                "tool": tool,
                "arguments": arguments,
                "payload_sha256": _digest(kept),
                "payload": kept,
            }
        )
        return payload

    def close(self) -> None:
        self._file.close()


class RecordedSource:
    """Answer from a recording. The newest entry for a call wins; nothing is fetched."""

    def __init__(self, path: Path) -> None:
        self._answers: dict[tuple[str, str], Payload] = {}
        self.recorded_at: str = self._load(path)

    def _load(self, path: Path) -> str:
        lines = path.read_text(encoding="utf-8").splitlines()
        try:
            header = json.loads(lines[0])
            entries = [json.loads(line) for line in lines[1:] if line.strip()]
        except (IndexError, ValueError) as exc:
            raise RecordingError(f"{path.name} is not a recording: {exc}") from exc
        if header.get("format") != FORMAT:
            raise RecordingError(f"{path.name} is not a dashboard recording")
        if header.get("version") != VERSION:
            raise RecordingError(f"{path.name} has unsupported version {header.get('version')!r}")
        latest = str(header.get("created_at", ""))
        for number, entry in enumerate(entries, start=2):
            try:
                tool, arguments, payload = entry["tool"], entry["arguments"], entry["payload"]
                matches = _digest(payload) == entry["payload_sha256"]
                stamp = str(entry["t"])
            except (KeyError, TypeError) as exc:
                raise RecordingError(f"{path.name} line {number} is malformed") from exc
            if not matches:
                raise RecordingError(f"{path.name} line {number} fails its payload hash")
            self._answers[(tool, _canonical(arguments))] = payload
            latest = max(latest, stamp)
        return latest

    async def call(self, tool: str, arguments: Payload) -> Payload:
        payload = self._answers.get((tool, _canonical(arguments)))
        if payload is not None:
            return json.loads(json.dumps(payload))  # type: ignore[no-any-return]
        return {
            "ok": False,
            "error": {
                "code": "not_recorded",
                "message": "This recording holds no answer for that request.",
                "hint": "Choose something visited while recording.",
            },
        }

"""Keep what was already fetched: a view cache, stale-while-revalidate, and an opt-in bulk load.

The complaint: panning lost the airports on screen. Filters never did (they are render-only);
a view change did, because each one fetched the airports nearest the new view and replaced the
old set. Here the server's half (the clock a kept view is aged by, the bulk load's chunks and
its stated price) and static pins on the page. The browser half (a pan held back by a route, a
failed refresh, panning back to a kept view, an expired one, and the bulk load stopped by a
refusal) is in `scripts/a11y.mjs`, `checkViewCache`.
"""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import re
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from dashboard import board
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.service import BoardService, View
from tests.test_aircraft_layer import NOW, STATUS, FakeSource, airport, world_service

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "dashboard" / "static"
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")
CACHE = (STATIC / "viewcache.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
GATE = (ROOT / "scripts" / "a11y.mjs").read_text(encoding="utf-8")
WORLD_VIEW = View(20.0, 10.0, 180.0)


def body(name: str) -> str:
    found = re.search(rf"\n  (?:async )?function {name}\(.*?\n  \}}\n", SCRIPT, re.S)
    assert found is not None, name
    return found.group()


def reported(category: str = "VFR", *, epoch: int | None = NOW) -> dict[str, Any]:
    return {
        "ok": True,
        "upstream_time_epoch": epoch,
        "metar": [{"flight_category": category, "observation_time": "2026-11-19T22:50:00Z"}],
        "latest_metar": {"age_s": 600, "current": True},
        "unknowns": [],
    }


REFUSED = {
    "ok": False,
    "error": {"code": "upstream_rate_limited", "message": "NOAA asked us to wait 60 s."},
}


def region_service(count: int, answer: Any) -> tuple[BoardService, FakeSource]:
    """`count` airports, far apart, each answered by `answer(ident)`."""
    records = [airport(f"A{n:04d}", (n % 60) * 2.5 - 70, n * 0.1) for n in range(count)]

    def overview(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "airports": {code: answer(code) for code in arguments["airports"]},
            "counts": {"requests": -(-len(arguments["airports"]) // 50)},
        }

    source = FakeSource(weather_overview=overview, upstream_status=STATUS)
    return BoardService(source, records), source


# --- the clock a kept view is aged by --------------------------------------------------------
async def test_a_board_answer_names_noaa_s_newest_clock_as_its_own() -> None:
    service, _ = world_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    assert snapshot["upstream_epoch"] == NOW
    assert board.newest_upstream([{"upstream_time": NOW - 5}, {"upstream_time": NOW}]) == NOW


async def test_an_answer_without_an_upstream_clock_has_no_age_and_is_never_fresh() -> None:
    service, _ = region_service(3, lambda _code: reported(epoch=None))

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    assert snapshot["upstream_epoch"] is None
    # Nothing falls back to this machine's clock, and a junk epoch is not a clock either.
    assert board.newest_upstream([{"upstream_time": True}, {"upstream_time": -1}, {}]) is None
    assert "const fresh = (age, ttl) => age !== null && age <= ttl;" in CACHE


async def test_the_cache_s_size_and_ttl_are_the_server_s() -> None:
    service, _ = world_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    assert snapshot["display"]["view_cache"] == {"views": 8, "ttl_s": 300}
    assert snapshot["display"]["metar_stale_after_s"] == 90 * 60
    # The page's fallback, until the first answer, is the same pair.
    assert "const VIEW_CACHE = { views: 8, ttl_s: 300 };" in SCRIPT


# --- the cache changes no payload field ------------------------------------------------------
async def test_the_cache_changes_no_payload_field() -> None:
    """The same assertion the filters carry: the server is never told, and nothing is written."""
    service, _ = world_service()

    first = await service.snapshot("world", view=WORLD_VIEW)
    kept = copy.deepcopy(first)
    second = await service.snapshot("world", view=WORLD_VIEW)

    assert first == second == kept
    # The page never writes into an answer it holds: no assignment to a field of one.
    # `row.dataset` is a DOM element in the badge, not an answer.
    holders = r"(?:board|payload|kept|entry|chunk|row|shown)(?!\.dataset)"
    assert not re.findall(rf"\b{holders}(?:\.\w+)+\s*(?:=(?!=)|\+=|-=)", SCRIPT)
    assert not re.findall(r"\bpayload(?:\.\w+)+\s*=(?!=)", CACHE)
    # A kept answer is stored and painted as the same object, never a re-stamped copy.
    assert "state.views.put(key, board, heard);" in body("refreshBoard")
    assert "applyBoard(kept.payload);" in body("refreshBoard")
    assert "upstream_epoch" not in body("applyBoard")
    # Nothing about the cache is sent: the view query and every path are what they were.
    assert not [p for p in re.findall(r"api\(`([^`]*)`", SCRIPT) if "cache" in p or "kept" in p]
    assert "views" not in body("viewQuery") and "shown" not in body("viewQuery")


# --- stale-while-revalidate ------------------------------------------------------------------
def test_a_view_change_leaves_the_drawing_until_the_answer_lands() -> None:
    refresh = body("refreshBoard")

    # Nothing is cleared on the way in: the airports on screen are replaced only by an answer.
    assert "state.airports =" not in refresh and "state.clusters =" not in refresh
    assert refresh.index('setViewStatus("updating", "");') < refresh.index("await api(")
    assert refresh.index("await api(") < refresh.index('setViewStatus("current", "");')
    # The view note's one word, and the age beside it.
    assert 'const VIEW_WORDS = { current: "", updating: "Updating", stale: "Stale" };' in SCRIPT
    note = body("renderViewState")
    assert "note.textContent = `${VIEW_WORDS[status]}: ${old} · ${line}`;" in note
    assert "`${ago(age)} old`" in note and '"age unknown"' in note


def test_a_failed_refresh_keeps_the_last_view_and_calls_it_stale() -> None:
    refresh = body("refreshBoard")
    failed = refresh[refresh.index("} catch {") :]

    # With a view on screen it stays, labelled; NO DATA is left for a board that never had one.
    assert failed.index('setViewStatus("stale", "failed");') < failed.index('"NO DATA"')
    note = body("renderViewState")
    assert 'renderBanner("STALE", `STALE: ${why}; the view on screen is ${old}`);' in note
    assert '"the board server did not answer"' in note
    # Its age is counted on each second, so a frozen number never goes on looking new.
    assert "timers.viewAge = setTimeout(renderViewState, 1000);" in note


def test_an_expired_kept_view_is_painted_only_when_nothing_else_is_and_then_as_stale() -> None:
    refresh = body("refreshBoard")

    assert "const fresh = BoardCache.fresh(age, cacheSettings().ttl_s);" in refresh
    assert "if (kept && (fresh || !state.shown)) {" in refresh
    assert 'setViewStatus(fresh ? "updating" : "stale", fresh ? "" : "expired");' in refresh
    # Found by the browser gate: a resize re-asked while an expired view was up, and the view
    # read "Updating" under its old LIVE banner. Whatever asks, past the TTL it is STALE.
    note = body("renderViewState")
    expired = 'state.viewStatus === "updating" && !BoardCache.fresh(age, cacheSettings().ttl_s)'
    assert expired in note
    assert '"a view with no upstream time"' in note
    # A hit is painted and the request still goes out: freshness is not traded for speed.
    assert refresh.index("applyBoard(kept.payload);") < refresh.index("await api(")
    # The region change leaves nothing of the old region on screen.
    region = re.search(r'\$\("region"\)\.addEventListener\("change".*?\n  \}\);', SCRIPT, re.S)
    assert region is not None and "state.shown = null;" in region.group()


def test_ages_are_counted_from_the_answers_own_clock() -> None:
    assert "state.upstream.hear(board.upstream_epoch, heard);" in body("refreshBoard")
    assert "return state.upstream.age(entry.payload.upstream_epoch, elapsed());" in body("keptAge")
    # The furthest-ahead reading wins, so a cached answer's older clock cannot pull it back.
    assert "if (this.offset === null || offset > this.offset) this.offset = offset;" in CACHE


def test_the_lru_keeps_eight_and_drops_the_least_recently_used() -> None:
    assert "while (this.entries.size > this.limit) this.entries.delete(" in CACHE
    # Keyed by region and the rounded view the server is asked for.
    assert "const key = `${region}${view}`;" in body("refreshBoard")
    assert "state.views.setLimit(cacheSettings(board).views);" in body("refreshBoard")
    assert '<script src="/static/viewcache.js"></script>' in PAGE
    assert PAGE.index("viewcache.js") < PAGE.index("board.js")


# --- the bulk load ---------------------------------------------------------------------------
def test_the_bulk_load_states_its_three_numbers() -> None:
    cost = board.bulk_cost(3270)

    assert cost == {
        "airports": 3270,
        "bytes": 1_242_600,
        "compressed_bytes": 163_500,
        "requests": 66,
        "seconds": 17,
        "chunk": 500,
        "label": "Load all 3,270 airports",
        "cost": "~1.2 MB (164 KB compressed) · ~66 weather requests · ~17 s",
    }
    # A small region never reads as nothing: one request, one second, a kilobyte at least.
    assert board.bulk_cost(1)["cost"] == "~1 KB (1 KB compressed) · ~1 weather request · ~1 s"
    assert board.bulk_cost(600)["cost"] == (
        "~228 KB (30 KB compressed) · ~12 weather requests · ~3 s"
    )


def test_sizes_read_in_kilobytes_then_megabytes() -> None:
    assert [board._size_text(size) for size in (0, 499, 500, 999_999, 1_000_000, 1_250_000)] == [
        "1 KB",
        "1 KB",
        "1 KB",
        "1000 KB",
        "1.0 MB",
        "1.2 MB",
    ]
    assert board._size_text(1_500) == "2 KB"


async def test_the_price_is_in_every_answer_and_nothing_is_spent_on_it() -> None:
    service, source = world_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    assert snapshot["bulk"] == board.bulk_cost(snapshot["viewport"]["region_total"])
    # The view asked about its 600 nearest, as before: the price is a sentence, not a load.
    asked = sum(len(a["airports"]) for tool, a in source.calls if tool == "weather_overview")
    assert asked == board.WEATHER_LOOKUP_CAP


def test_the_stated_price_is_not_under_the_synthetic_measurement() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from a11y_server import SyntheticSource, listed_airports  # noqa: PLC0415

    service = BoardService(SyntheticSource("data"), listed_airports())

    async def load() -> tuple[int, int, int, int]:
        size = packed = rows = requests = 0
        offset: int | None = 0
        while offset is not None:
            chunk = await service.bulk("world", offset)
            raw = json.dumps(chunk, separators=(",", ":")).encode()
            size, packed = size + len(raw), packed + len(gzip.compress(raw))
            rows, offset = rows + chunk["got"], chunk["next"]
            requests += 1
        return size, packed, rows, requests

    size, packed, rows, chunks = asyncio.run(load())

    assert rows == 3270
    assert chunks == 7
    assert size <= board.bulk_cost(rows)["bytes"]
    assert packed <= board.bulk_cost(rows)["compressed_bytes"]


async def test_a_bulk_load_walks_the_region_in_code_order_chunk_by_chunk() -> None:
    service, source = region_service(1100, lambda _code: reported())

    chunks = []
    offset: int | None = 0
    while offset is not None:
        chunks.append(await service.bulk("world", offset))
        offset = chunks[-1]["next"]

    assert [(c["offset"], c["asked"], c["got"], c["next"]) for c in chunks] == [
        (0, 500, 500, 500),
        (500, 500, 500, 1000),
        (1000, 100, 100, None),
    ]
    assert [c["requests"] for c in chunks] == [10, 10, 2]
    codes = [row["icao"] for c in chunks for row in c["airports"]]
    assert codes == sorted(codes) and len(set(codes)) == 1100
    assert all(c["refused"] is None and c["total"] == 1100 for c in chunks)
    # Each chunk is asked of the overview a hundred at a time, as a view is.
    assert {len(a["airports"]) for tool, a in source.calls if tool == "weather_overview"} == {100}


async def test_every_bulk_row_keeps_its_own_age_and_the_clock_that_measured_it() -> None:
    def answer(code: str) -> dict[str, Any]:
        return reported(epoch=NOW - 30) if code == "A0001" else reported()

    service, _ = region_service(3, answer)

    chunk = await service.bulk("world")

    rows = {row["icao"]: row for row in chunk["airports"]}
    assert rows["A0000"]["observed"] == {"age_s": 600, "upstream_epoch": NOW}
    assert rows["A0001"]["observed"] == {"age_s": 600, "upstream_epoch": NOW - 30}
    assert chunk["upstream_epoch"] == NOW
    # The row is the map's own row, plus that one field.
    assert {k: v for k, v in rows["A0000"].items() if k != "observed"} == board.board_airport(
        airport("A0000", -70.0, 0.0),
        {"summary": "VFR, METAR 10 min old", "neighbour": None},
        board.marker({"state": "reported", "category": "VFR"}),
    )


async def test_a_rate_limit_stops_the_load_and_says_how_many_it_got() -> None:
    def answer(code: str) -> dict[str, Any]:
        return REFUSED if int(code[1:]) >= 650 else reported()

    service, _ = region_service(1100, answer)

    first = await service.bulk("world", 0)
    second = await service.bulk("world", first["next"])

    assert (first["got"], first["next"], first["refused"]) == (500, 500, None)
    # The refused airports are left out, the answered ones kept, and nothing follows.
    assert (second["asked"], second["got"], second["next"]) == (500, 150, None)
    assert second["refused"] == "NOAA asked us to wait 60 s."
    assert all(int(row["icao"][1:]) < 650 for row in second["airports"])


async def test_a_whole_call_refused_by_the_ledger_is_a_refusal_too() -> None:
    ledger = {"ok": False, "error": {"code": "local_budget_exhausted", "message": "over budget"}}
    service, _ = region_service(10, lambda _code: reported())
    service._source = FakeSource(weather_overview=ledger, upstream_status=STATUS)  # type: ignore[assignment]

    chunk = await service.bulk("world")

    assert (chunk["got"], chunk["next"], chunk["refused"], chunk["airports"]) == (
        0,
        None,
        "over budget",
        [],
    )
    # A failure that is not a refusal is an Unknown row, not a stop.
    service._source = FakeSource(  # type: ignore[assignment]
        weather_overview={"ok": False, "error": {"code": "upstream_unavailable"}},
        upstream_status=STATUS,
    )
    failed = await service.bulk("world")
    assert (failed["got"], failed["refused"]) == (10, None)
    assert {row["cat"] for row in failed["airports"]} == {"U"}


def test_the_bulk_route_is_a_post_with_a_bounded_chunk() -> None:
    service, _ = region_service(3, lambda _code: reported())
    client = TestClient(create_app(service, Gate(None)), base_url="http://127.0.0.1")

    assert client.post("/api/bulk/world").json()["got"] == 3
    assert client.post("/api/bulk/world?offset=2&limit=1").json()["airports"][0]["icao"] == "A0002"
    # A GET spends nothing: a prefetch or a crawler cannot start a load.
    assert client.get("/api/bulk/world").status_code == 405
    assert client.post(f"/api/bulk/world?limit={board.BULK_CHUNK + 1}").status_code == 422
    assert client.post("/api/bulk/mars").status_code == 404


def test_the_bulk_load_is_opt_in_and_remembered_for_the_session_only() -> None:
    assert (
        '<button id="bulk-load" class="quiet" type="button" aria-pressed="false"'
        ' aria-describedby="bulk-cost">' in PAGE
    )
    # Called only by the button, and by a load this tab session already asked for.
    callers = re.findall(r"\bloadAll\(\);", SCRIPT)
    assert len(callers) == 2
    assert "if (sessionChoice(BULK_KEY) === region && !state.bulk.loading" in body("refreshBoard")
    assert "loadAll" not in body("startPolling")
    assert "rememberSession(BULK_KEY" in SCRIPT and not re.search(r"remember\(BULK_KEY", SCRIPT)
    region = re.search(r'\$\("region"\)\.addEventListener\("change".*?\n  \}\);', SCRIPT, re.S)
    assert region is not None
    assert 'rememberSession(BULK_KEY, "");' in region.group() and "dropBulk();" in region.group()


def test_a_bulk_row_is_aged_on_and_never_called_current_past_the_limit() -> None:
    row = body("bulkRow")

    assert "const since = state.upstream.age(observed.upstream_epoch, at);" in row
    assert "observed.age_s + since > limit" in row
    # Past the limit it loses its marker and its letter: drawn and filtered as Unknown.
    assert 'cat: "U",' in row and "marker" not in row.split("return {", 1)[1].split("};")[0]
    # The view's own answer wins for its airports.
    assert "if (!seen.has(row.icao)) rows.push(bulkRow(row, limit, at));" in body("boardRows")


def test_a_partial_load_is_labelled_and_never_silently_complete() -> None:
    note = body("bulkNote")

    assert "`Stopped at ${of}: ${bulk.stopped}. The rest are not loaded.`" in note
    assert "if (chunk.refused) { finishBulk(chunk.refused); return; }" in body("loadAll")
    assert 'if (!chunk) { finishBulk("the board server did not answer"); return; }' in body(
        "loadAll"
    )
    assert "checkViewCache" in GATE


def test_bulk_rows_cluster_on_the_server_s_grid() -> None:
    cells = body("allCells")

    assert "Math.floor((airport.latitude + 90.0) / cellDeg)" in cells
    assert "Math.floor((airport.longitude + 180.0) / cellDeg)" in cells
    # Ties go to the worse category, as board.clusters does.
    assert "counts[c] >= counts[majority]" in cells
    # board.cluster_text's wording, so a bulk cluster reads like a view's.
    counts = {"VFR": 2, "MVFR": 0, "IFR": 1, "LIFR": 0}
    text = board.cluster_text(counts, 3, show_unknown=True)
    assert text == "◆ 1 IFR · ● 2 VFR · ○ 3 without a report"
    assert "`○ ${unknown} without a report`" in body("clusterText")

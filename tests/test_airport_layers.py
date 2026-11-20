"""Airports off, aircraft off, and airports by weather: the decluttering controls.

The owner's complaint: over Europe and North America the airport layer is dense enough that
single aircraft cannot be picked out. The server adds one letter per airport row, `cat`, so
the page can filter by weather; everything else here is a render decision in the page, held
by the static checks below in the style of the other `board.js` tests.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any

from dashboard import board
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.service import BoardService, View
from tests.test_aircraft_layer import NOW, STATUS, FakeSource, airport, world_service

STATIC = Path(__file__).parent.parent / "dashboard" / "static"
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
WORLD_VIEW = View(20.0, 10.0, 180.0)


def body(name: str) -> str:
    """A named function's body in board.js, as the other static tests read it."""
    found = re.search(rf"function {name}\([^)]*\) \{{(.*?)\n  \}}", SCRIPT, re.S)
    assert found, f"{name} is not in board.js"
    return found.group(1)


def metar(category: str, *, age_s: int = 600, current: bool = True) -> dict[str, Any]:
    return {
        "ok": True,
        "upstream_time_epoch": NOW,
        "metar": [{"flight_category": category, "raw_text": f"{category} METAR"}],
        "latest_metar": {"age_s": age_s, "current": current},
        "unknowns": [],
    }


SILENT = {"ok": True, "upstream_time_epoch": NOW, "metar": [], "latest_metar": {}, "unknowns": []}
# One airport of each kind. The three unknowns are three different reasons: NOAA held
# nothing, the report is old, and the fetch failed (the overview had no entry for it).
KINDS = {
    "AVFR": metar("VFR"),
    "BMVF": metar("MVFR"),
    "CIFR": metar("IFR"),
    "DLIF": metar("LIFR"),
    "ENOB": SILENT,
    "FOLD": metar("VFR", age_s=4 * 3600, current=False),
    # "GFAI" is left out of the answer: the fetch for it failed.
}
EXPECTED = {
    "AVFR": "V",
    "BMVF": "M",
    "CIFR": "I",
    "DLIF": "L",
    "ENOB": "U",
    "FOLD": "U",
    "GFAI": "U",
}


def kinds_service() -> BoardService:
    # Far enough apart that no airport borrows a neighbour's line.
    records = [airport(ident, 10.0 * n, 10.0 * n) for n, ident in enumerate(EXPECTED)]

    def overview(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "airports": {i: KINDS[i] for i in arguments["airports"] if i in KINDS},
        }

    source = FakeSource(weather_overview=overview, upstream_status=STATUS)
    return BoardService(source, records)


# --- the one new payload field ---------------------------------------------------------------
async def test_every_airport_row_carries_one_letter_of_weather() -> None:
    service, _ = world_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    letters = [row["cat"] for row in snapshot["airports"]]
    assert len(letters) == 600
    assert set(letters) <= {"V", "M", "I", "L", "U"}
    # A trimmed row keeps it: the filter cannot be built on a field the payload does not carry.
    trimmed = [row for row in snapshot["airports"] if "marker" not in row]
    assert trimmed and all(
        set(row) == {"icao", "name", "latitude", "longitude", "cat"} for row in trimmed
    )
    assert {row["cat"] for row in trimmed} == {"U"}


async def test_three_kinds_of_unknown_are_one_letter_and_keep_their_own_reasons() -> None:
    service = kinds_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    assert {row["icao"]: row["cat"] for row in snapshot["airports"]} == EXPECTED
    # The letter does not say why. The airport's own line still does, where it already was.
    listed = service.airport_list("world", limit=50)["items"]
    reasons = {row["icao"]: row["weather"]["summary"] for row in listed}
    assert reasons["ENOB"] == "Unknown: no METAR returned"
    assert reasons["FOLD"].startswith("Unknown: METAR 4 h") and reasons["FOLD"].endswith("old")
    assert reasons["GFAI"] == "Unknown: weather unavailable (not_answered)"


def test_the_letter_follows_the_marker_and_nothing_else() -> None:
    for category, letter in board.CATEGORY_LETTER.items():
        reported = board.marker({"state": "reported", "category": category})
        assert board.category_letter(reported) == letter
    assert board.category_letter(board.marker({"state": "unknown"})) == "U"
    # A marker that somehow named a fifth category is not coloured by a letter either.
    assert board.category_letter({"kind": "weather", "category": "SPECIAL"}) == "U"
    assert board.category_letter({"kind": "weather", "category": None}) == "U"


async def test_the_chip_counts_equal_the_counts_in_the_airport_rows() -> None:
    service = kinds_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    chips = snapshot["weather_filter"]
    tally = dict.fromkeys(board.WEATHER_FILTER_ORDER, 0)
    for row in snapshot["airports"]:
        tally[row["cat"]] += 1
    assert chips["counts"] == tally == {"V": 1, "M": 1, "I": 1, "L": 1, "U": 3}
    assert chips["all"] == len(snapshot["airports"]) == 7
    assert chips["labels"] == {"V": "VFR", "M": "MVFR", "I": "IFR", "L": "LIFR", "U": "Unknown"}
    # Every letter is listed, zero or not, in the order the chips are drawn.
    assert list(chips["counts"]) == ["V", "M", "I", "L", "U"]


async def test_the_world_chip_counts_add_up_to_what_was_received() -> None:
    service, _ = world_service()

    snapshot = await service.snapshot("world", view=WORLD_VIEW)

    chips = snapshot["weather_filter"]
    assert sum(chips["counts"].values()) == chips["all"] == 600
    assert chips["counts"]["U"] == snapshot["hidden"]["unknown"] == 400


# --- render-only, pinned ---------------------------------------------------------------------
async def test_filtering_by_weather_and_switching_layers_change_no_payload_field() -> None:
    """The chips and the switches are render decisions in the page: the server is never told."""
    service, _ = world_service()

    first = await service.snapshot("world", view=WORLD_VIEW)
    second = await service.snapshot("world", view=WORLD_VIEW)

    assert first == second
    # The field is in the payload whether or not anything is filtered.
    assert "weather_filter" in first
    assert all("cat" in row for row in first["airports"])
    routes = {r.path: r for r in create_app(service, Gate(None)).routes}  # type: ignore[attr-defined]
    params = inspect.signature(routes["/api/board"].endpoint).parameters  # type: ignore[attr-defined]
    assert not [n for n in params if any(w in n for w in ("weather", "cat", "layer", "flights"))]
    for path in re.findall(r"api\(`([^`]*)`", SCRIPT):
        assert not [w for w in ("weather", "cat=", "layer", "flights") if w in path.lower()], path
    sent = body("viewQuery")
    assert "weatherFilter" not in sent and "layers" not in sent


def test_the_choices_are_kept_for_the_session_and_the_chip_dropped_with_the_region() -> None:
    assert 'const WEATHER_KEY = "airport-board.weather-filter";' in SCRIPT
    assert 'const LAYERS_KEY = "airport-board.layers";' in SCRIPT
    # Session, never the browser: neither key is written to localStorage.
    assert "rememberSession(WEATHER_KEY" in SCRIPT and "rememberSession(LAYERS_KEY" in SCRIPT
    assert not re.search(r"remember\((WEATHER_KEY|LAYERS_KEY)", SCRIPT)
    wiring = r'\$\("region"\)\.addEventListener\("change", \(\) => \{(.*?)\n  \}\);'
    region = re.search(wiring, SCRIPT, re.S)
    assert region
    assert 'state.weatherFilter = "";' in region.group(1)
    assert 'rememberWeather("");' in region.group(1)


# --- the layers ------------------------------------------------------------------------------
def test_the_airports_switch_takes_markers_clusters_counts_and_hit_targets_together() -> None:
    draw = body("draw")
    # Clusters (their markers, counts and pickables) and single airports (markers, codes and
    # pickables) are drawn only through these two calls, and both wait for the switch.
    assert "const cells = airports ? clustered() : null;" in draw
    assert "if (cells) drawClusters(cells);" in draw
    assert "if (airports) drawAirports();" in draw
    assert SCRIPT.count("drawClusters(") == 2 and SCRIPT.count("drawAirports(") == 2
    # Their hit targets exist only where they were drawn: rebuilt each frame from the drawing.
    assert "state.pickables = [];" in draw
    assert re.findall(r'kind: "(airport|cluster)"', SCRIPT) == ["airport", "cluster"]
    for name, kind in (("drawAirport", "airport"), ("drawClusters", "cluster")):
        assert f'kind: "{kind}"' in body(name)
    # The aircraft layer does not depend on the airport switch, so it is still picked.
    aircraft_branch = draw.split("} else if (layer) {", 1)[1].split("} else if", 1)[0]
    assert "drawAircraft(aircraft, dense, dim, true)" in aircraft_branch
    assert "airports" not in aircraft_branch


def test_the_aircraft_switch_hides_every_traffic_mark() -> None:
    draw = body("draw")
    assert "if (!aircraft) {" in draw
    assert "if (aircraft && state.focus) drawTransit(path);" in draw
    assert "if (aircraft && state.focus) { drawTrail(state.focus); drawFocused(); }" in draw
    # drawInbound and the detail's aircraft sit in the branches after `if (!aircraft)`.
    hidden = draw.split("if (!aircraft) {", 1)[1].split("if (airports) drawAirports();", 1)[0]
    assert "drawInbound(path)" in hidden and "state.detail.traffic.aircraft" in hidden


def test_both_layers_off_is_said_in_words() -> None:
    note = body("layerNote")
    assert 'if (!airports && !aircraft) return "Nothing drawn: both layers are off";' in note
    assert "renderLayerNote();" in body("draw")
    assert 'id="layer-note"' in PAGE and 'role="status"' in PAGE.split('id="layer-note"', 1)[1][:40]


def test_an_empty_filter_is_a_statement_not_a_blank_map() -> None:
    note = body("layerNote")
    assert "No airports match: 0 of ${state.weather.all" in note


def test_with_airports_off_the_drawn_count_says_the_layer_is_hidden() -> None:
    toggle = body("renderUnknownToggle")
    assert "if (!state.layers.airports) {" in toggle
    assert "Airport layer off: none of ${received" in toggle
    # The view note, which counts the data, is untouched by the switch.
    assert "layers" not in body("refreshBoard")


def test_flights_only_is_reversible_and_restores_the_switches_as_they_were() -> None:
    preset = body("flightsOnly")
    assert "if (layers.before) return { ...layers.before, before: null };" in preset
    assert "airports: false, aircraft: true," in preset
    assert "before: { airports: layers.airports, aircraft: layers.aircraft }," in preset
    # A switch pressed by hand ends the preset, so a later press does not restore stale state.
    assert "({ ...layers, [name]: !layers[name], before: null })" in SCRIPT
    # The pressed state is what the button reports.
    assert '$("flights-only").setAttribute("aria-pressed", String(Boolean(before)));' in SCRIPT


def test_the_preset_round_trips_every_starting_state() -> None:
    """The same rule as `flightsOnly`, run over all four starting states."""

    def flights_only(layers: dict[str, Any]) -> dict[str, Any]:
        if layers["before"]:
            return {**layers["before"], "before": None}
        before = {"airports": layers["airports"], "aircraft": layers["aircraft"]}
        return {"airports": False, "aircraft": True, "before": before}

    for airports in (True, False):
        for aircraft in (True, False):
            start = {"airports": airports, "aircraft": aircraft, "before": None}
            pressed = flights_only(start)
            assert (pressed["airports"], pressed["aircraft"]) == (False, True)
            assert flights_only(pressed) == start
    # And the page holds that rule, word for word in its two branches.
    assert "{ ...layers.before, before: null }" in body("flightsOnly")


# --- the filter on the clusters --------------------------------------------------------------
def test_the_filter_hides_a_cell_left_empty_and_keeps_one_with_matches() -> None:
    """A filtered group is board.clusters over the matching rows alone; the page mirrors it."""

    def row(ident: str, lat: float, lon: float, category: str | None) -> dict[str, Any]:
        weather = {"state": "reported", "category": category} if category else {"state": "unknown"}
        return board.board_airport(airport(ident, lat, lon), weather, board.marker(weather))

    rows = [
        row("A", 50.2, 10.2, "VFR"),
        row("B", 51.8, 11.8, "IFR"),
        row("C", 52.0, 13.0, "VFR"),
        row("D", -32.0, 100.0, "VFR"),
        row("E", -33.0, 101.0, None),
    ]
    everything = board.clusters(rows, 10.0)
    ifr = board.clusters([r for r in rows if r["cat"] == "I"], 10.0)

    assert len(everything) == 2
    # The southern cell holds no IFR: it is not drawn at all. The northern one still has one.
    assert len(ifr) == 1
    (north,) = ifr
    assert (north["reported"], north["unknown"], north["majority"], north["worst"]) == (
        1,
        0,
        "IFR",
        "IFR",
    )
    assert north["text"] == "◆ 1 IFR"
    # Placed at its matching airport, not at the mean of the cell's VFR ones.
    assert (north["latitude"], north["longitude"]) == (51.8, 11.8)
    # The page groups with the same grid and the same keys, and draws only what it grouped.
    cells = body("filteredCells")
    assert "if (airport.cat !== letter) continue;" in cells
    assert "Math.floor((airport.latitude + 90.0) / cellDeg)" in cells
    assert "Math.floor((airport.longitude + 180.0) / cellDeg)" in cells
    assert "chosen ? filteredCells(cell, chosen) : state.clusters[String(cell)]" in body(
        "clustered"
    )
    assert "if (!count) continue;" in body("drawClusters")


def test_the_page_s_symbols_and_letters_are_the_server_s() -> None:
    for category, symbol in board.CATEGORY_SYMBOL.items():
        assert f'{category}: {{ colour: token("--{category.lower()}"),' in SCRIPT
        assert f'symbol: "{symbol}" }}' in SCRIPT.split(f"{category}: {{", 1)[1].split("\n", 1)[0]
    letters = re.search(r"const LETTER_CATEGORY = (\{[^}]*\});", SCRIPT)
    assert letters
    parsed = json.loads(re.sub(r"(\w+):", r'"\1":', letters.group(1)))
    assert parsed == {letter: category for category, letter in board.CATEGORY_LETTER.items()}


def test_the_filter_applies_to_single_airports_by_their_letter() -> None:
    airports = body("drawAirports")
    assert "chosen ? airport.cat === chosen : Boolean(airport.marker) || show" in airports
    # The Unknown chip draws the airports without a report whatever the toggle says.
    assert 'return chosen ? chosen === "U" : unknownShown();' in body("unknownDrawn")
    assert "const show = unknownDrawn();" in body("drawClusters")


# --- the controls ----------------------------------------------------------------------------
def test_the_new_controls_are_buttons_that_report_their_state() -> None:
    for ident, label, pressed in (
        ("layer-airports", "Airports", "true"),
        ("layer-aircraft", "Aircraft", "true"),
        ("flights-only", "Flights only", "false"),
    ):
        tag = re.search(rf'<button id="{ident}"[^>]*>{label}</button>', PAGE)
        assert tag, ident
        assert 'type="button"' in tag.group(0) and f'aria-pressed="{pressed}"' in tag.group(0)
    assert 'aria-label="Layers drawn on the map"' in PAGE
    assert 'aria-label="Show airports by weather"' in PAGE
    chip = body("weatherChip")
    assert 'button.setAttribute("aria-pressed", String(state.weatherFilter === value));' in chip
    # Each chip has an id, so a re-render can give the keyboard back to the same one.
    assert 'button.id = `weather-chip-${value || "all"}`;' in chip
    assert "if (focused && $(focused)) $(focused).focus();" in body("replaceChips")


def test_the_controls_come_after_the_unknown_toggle_in_the_tab_order() -> None:
    order = [
        PAGE.index(f'id="{ident}"')
        for ident in (
            "unknown-on",
            "layer-airports",
            "layer-aircraft",
            "flights-only",
            "weather-chips",
        )
    ]
    assert order == sorted(order)
    assert "tabindex" not in PAGE.split('id="layers"', 1)[1].split("</div>", 1)[0]

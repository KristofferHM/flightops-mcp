"""What approaches an airport: geometry, ETA, the cap, the arrival board, the priced tracks.

And the page's half: the four ways out of an aircraft selection, held as code, since there is
no browser in CI.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from dashboard import inbound
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.inbound import (
    CONVERGENCE_DEG,
    DRAWN_MAX,
    RADIUS_NM,
    convergent,
    eta_minutes,
    inbound_view,
    off_course_deg,
)
from dashboard.service import BoardService, UnknownBoardItem
from tests.test_aircraft_layer import LAYER, NOW, STATUS, TRACK_PAYLOAD, WORLD, FakeSource, plane

ESGG = {"icao": "ESGG", "latitude": 57.6628, "longitude": 12.2798}
STATIC = Path(__file__).parent.parent / "dashboard" / "static"
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")


def out_from(bearing: float, nm: float, origin: dict[str, Any] = ESGG) -> tuple[float, float]:
    """The point `nm` from the airport along `bearing`, on the sphere."""
    d = math.radians(nm / 60.0)
    b = math.radians(bearing)
    p = math.radians(origin["latitude"])
    lat = math.asin(math.sin(p) * math.cos(d) + math.cos(p) * math.sin(d) * math.cos(b))
    lon = math.radians(origin["longitude"]) + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p), math.cos(d) - math.sin(p) * math.sin(lat)
    )
    return math.degrees(lat), math.degrees(lon)


def approaching(icao24: str, bearing: float, nm: float, **fields: Any) -> dict[str, Any]:
    """An aircraft `nm` out on `bearing` from the airport, flying straight at it."""
    lat, lon = out_from(bearing, nm)
    return plane(icao24, latitude=lat, longitude=lon, track=(bearing + 180) % 360, **fields)


def ids(view: dict[str, Any]) -> list[str]:
    return [row["icao24"] for row in view["aircraft"]]


# --- convergence ---------------------------------------------------------------------------
def test_an_aircraft_20_nm_out_tracking_at_the_airport_is_approaching() -> None:
    view = inbound_view([approaching("aaa001", 180, 20)], ESGG, None)

    assert ids(view) == ["aaa001"]
    row = view["aircraft"][0]
    assert row["distance_nm"] == pytest.approx(20, abs=0.2)
    assert row["evidence"] == "geometry"
    assert "projected, not observed" in row["text"]


def test_the_same_aircraft_tracking_90_degrees_away_is_not() -> None:
    entry = approaching("aaa001", 180, 20)
    entry["track"] = 90.0

    assert inbound_view([entry], ESGG, None)["aircraft"] == []


def test_an_aircraft_on_the_ground_is_not_and_neither_is_an_unknown_phase() -> None:
    grounded = approaching("aaa001", 180, 20, on_ground=True)
    unknown = approaching("aaa002", 180, 20, on_ground=None)

    view = inbound_view([grounded, unknown], ESGG, None)

    assert view["aircraft"] == []
    assert view["within_radius"] == 2


def test_an_aircraft_61_nm_out_is_not_and_one_at_59_is() -> None:
    view = inbound_view(
        [approaching("aaa061", 180, 61), approaching("aaa059", 180, 59)], ESGG, None
    )

    assert ids(view) == ["aaa059"]
    assert view["within_radius"] == 1


@pytest.mark.parametrize(("track", "bearing"), [(350.0, 10.0), (10.0, 350.0), (355.0, 5.0)])
def test_the_bearing_wraps_at_north(track: float, bearing: float) -> None:
    assert off_course_deg(track, bearing) == pytest.approx(
        abs(((track - bearing + 180) % 360) - 180)
    )
    assert off_course_deg(track, bearing) <= 20


def test_the_wrap_holds_on_the_map_too() -> None:
    """Out to the south-south-east, the airport bears about 350°; a track of 10° converges."""
    lat, lon = out_from(170, 20)
    on_wrap = plane("aaa001", latitude=lat, longitude=lon, track=10.0)
    beyond = plane("aaa002", latitude=lat, longitude=lon, track=10.0 + CONVERGENCE_DEG + 1)

    view = inbound_view([on_wrap, beyond], ESGG, None)

    assert ids(view) == ["aaa001"]
    assert view["aircraft"][0]["bearing_to_airport_deg"] in range(348, 353)


def test_no_track_is_not_convergent() -> None:
    entry = approaching("aaa001", 180, 20)
    entry["track"] = None

    assert inbound_view([entry], ESGG, None)["aircraft"] == []
    assert not convergent({**entry, "distance_nm": 20.0, "bearing_to_airport_deg": 0})


def test_an_aircraft_without_a_position_is_skipped() -> None:
    lost = plane("aaa001", latitude=None)

    assert inbound_view([lost], ESGG, None)["within_radius"] == 0


# --- ETA -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("nm", "speed", "on_ground", "minutes"),
    [
        # 200 m/s is 388.8 kt: 20 nm is 3.09 min, rounded to 3.
        (20.0, 200.0, False, 3),
        (60.0, 128.6, False, 14),
        # Overhead still says at least a minute, never 0.
        (0.1, 250.0, False, 1),
        (20.0, 0.0, False, None),
        (20.0, -3.0, False, None),
        (20.0, None, False, None),
        (20.0, "fast", False, None),
        (20.0, True, False, None),
        (20.0, 200.0, True, None),
        (20.0, 200.0, None, None),
    ],
)
def test_the_eta_is_distance_over_ground_speed_and_never_from_an_unknown(
    nm: float, speed: object, on_ground: object, minutes: int | None
) -> None:
    assert eta_minutes(nm, speed, on_ground) == minutes


def test_the_eta_is_labelled_estimated_and_its_absence_says_why() -> None:
    fast = inbound_view([approaching("aaa001", 180, 20)], ESGG, None)["aircraft"][0]
    stopped = inbound_view([approaching("aaa002", 180, 20, velocity_ms=None)], ESGG, None)

    assert fast["eta_min"] == 3
    assert "ETA about 3 min, estimated from ground speed and distance" in fast["text"]
    assert stopped["aircraft"][0]["eta_min"] is None
    assert "no ETA: ground speed unknown or zero" in stopped["aircraft"][0]["text"]


# --- the cap and the counts ----------------------------------------------------------------
def test_27_candidates_draw_12_and_the_text_counts_the_real_ones() -> None:
    near = [approaching(f"b{n:05x}", n * 13 % 360, 5 + n, velocity_ms=200.0) for n in range(27)]
    crossing = [
        {**approaching(f"c{n:05x}", 90, 30), "track": 0.0} for n in range(4)
    ]  # within 60 nm, not convergent

    view = inbound_view(near + crossing, ESGG, None)

    assert view["candidates"] == 27
    assert view["drawn"] == DRAWN_MAX == 12
    assert view["within_radius"] == 31
    assert view["text"] == "27 approaching · 31 within 60 nm"
    assert view["dropped"] == "drawing the nearest 12 of 27"
    distances = [row["distance_nm"] for row in view["aircraft"]]
    assert distances == sorted(distances) and distances[-1] < 18
    assert view["price"] == {
        "count": 12,
        "each": 4,
        "total": 48,
        "text": "Show observed tracks (12 aircraft · 4 credits each = 48)",
    }


def test_the_thresholds_are_stated_with_the_result() -> None:
    view = inbound_view([], ESGG, None)

    assert view["thresholds"] == {"radius_nm": 60.0, "convergence_deg": 30.0, "drawn_max": 12}
    assert view["rule"] == (
        "Approaching means airborne, within 60 nm of ESGG, with its track within ±30° of the "
        "bearing to ESGG. The nearest 12 are drawn. Each line is projected, not observed."
    )
    assert view["dropped"] is None
    assert view["text"] == "0 approaching · 0 within 60 nm"
    assert (RADIUS_NM, CONVERGENCE_DEG) == (60.0, 30.0)


def test_a_tie_in_distance_breaks_by_icao24() -> None:
    twins = [approaching("bbb002", 180, 20), approaching("bbb001", 180, 20)]

    assert ids(inbound_view(twins, ESGG, None)) == ["bbb001", "bbb002"]


# --- the arrival board ---------------------------------------------------------------------
def test_a_callsign_on_the_arrival_board_is_evidence_and_counted_apart() -> None:
    on_list = approaching("ddd001", 180, 30, callsign="SAS123 ")
    geometric = approaching("ddd002", 90, 20, callsign="NOZ1")
    # On the list, airborne, inside the radius but not pointed at the field: still evidence.
    holding = {**approaching("ddd003", 0, 15, callsign="FIN7"), "track": 90.0}

    view = inbound_view([on_list, geometric, holding], ESGG, ["sas123", "FIN7", "DLH9"])
    rows = {row["icao24"]: row for row in view["aircraft"]}

    assert rows["ddd001"]["evidence"] == "arrival_board"
    assert rows["ddd003"]["evidence"] == "arrival_board"
    assert rows["ddd002"]["evidence"] == "geometry"
    assert "on the arrival board" in rows["ddd001"]["text"]
    assert (view["on_board"], view["approaching"]) == (2, 1)
    assert view["text"] == "2 on the arrival board · 1 more approaching · 3 within 60 nm"
    assert view["board"] == "asked" and view["board_text"] is None


def test_a_callsign_missing_from_the_board_keeps_its_geometric_place() -> None:
    view = inbound_view([approaching("eee001", 180, 20, callsign="BAW1")], ESGG, ["SAS9"])

    assert ids(view) == ["eee001"]
    assert view["aircraft"][0]["evidence"] == "geometry"
    assert view["text"] == "0 on the arrival board · 1 more approaching · 1 within 60 nm"


def test_a_landed_aircraft_on_the_board_is_not_approaching() -> None:
    landed = approaching("fff001", 180, 2, callsign="SAS1", on_ground=True)

    view = inbound_view([landed], ESGG, ["SAS1"])

    assert view["aircraft"] == [] and view["on_board"] == 0


def test_without_the_board_the_text_says_it_was_not_checked() -> None:
    view = inbound_view([approaching("aaa001", 180, 20)], ESGG, None)

    assert view["board"] == "not_asked"
    assert view["board_text"] == "Arrival board not checked."
    assert view["text"] == "1 approaching · 1 within 60 nm"


@pytest.mark.parametrize(
    ("payload", "state", "callsigns", "text"),
    [
        (
            {
                "ok": True,
                "window": {"minutes": 60},
                "movements": {"arrival": [{"callsign": "SAS1 "}, {"callsign": None}]},
            },
            "read",
            ["SAS1"],
            "1 on OpenSky's arrival list in the last 60 min. OpenSky lists a flight once it has "
            "landed, so an aircraft still flying is usually not on it.",
        ),
        (
            {"ok": True, "movements": {}, "unknowns": ["arrival data unavailable: no creds."]},
            "unavailable",
            None,
            "Arrival board unavailable: arrival data unavailable: no creds.",
        ),
        (
            {"ok": False, "error": {"code": "local_budget_exhausted", "message": "Guard said no."}},
            "unavailable",
            None,
            "Arrival board unavailable: Guard said no.",
        ),
        ({"ok": False}, "unavailable", None, "Arrival board unavailable: no arrival list returned"),
    ],
)
def test_the_arrival_board_is_read_or_says_why_not(
    payload: dict[str, Any], state: str, callsigns: list[str] | None, text: str
) -> None:
    view = inbound.board_view(payload)

    assert (view["state"], view["callsigns"], view["text"]) == (state, callsigns, text)


# --- the price, and refusals as data -------------------------------------------------------
@pytest.mark.parametrize(
    ("count", "remaining", "refused"),
    [
        (8, 11, "Not enough credits for 8 tracks (32 needed, 11 remaining)."),
        (1, 3, "Not enough credits for 1 track (4 needed, 3 remaining)."),
        (2, -5, "Not enough credits for 2 tracks (8 needed, 0 remaining)."),
        (8, 32, None),
        (8, None, None),
        (0, 100, "Nothing is approaching, so there are no tracks to show."),
    ],
)
def test_a_refusal_states_the_arithmetic(count: int, remaining: int | None, refused: str) -> None:
    assert inbound.tracks_refusal(count, remaining) == refused


@pytest.mark.parametrize(
    ("governance", "left"),
    [
        ({"credits_hour": 389, "budget_hour": 400}, 11),
        ({"credits_hour": 10, "budget_hour": 400, "credits_24h": 990, "budget_24h": 1000}, 10),
        ({"credits_hour": None, "budget_hour": 400}, None),
        ({}, None),
    ],
)
def test_the_fewest_credits_any_guard_has_left(governance: dict[str, Any], left: int) -> None:
    assert inbound.remaining_credits(governance) == left


# --- the service and its routes ------------------------------------------------------------
def inbound_layer(count: int) -> dict[str, Any]:
    """`count` aircraft converging on ESGG, plus the shared layer's 300 moved far away."""
    rows = []
    for n in range(count):
        lat, lon = out_from(180, 10 + n)
        rows.append([f"a{n:05x}", f"SAS{n + 10}", lat, lon, 3000.0, False, 150.0, 0.0, NOW - 5])
    # The shared rows sit beside ESGG flying east, and some of them converge on it.
    far = [[*row[:3], -40.0, *row[4:]] for row in LAYER["rows"]]
    return {**LAYER, "rows": rows + far}


def service_for(count: int, **answers: Any) -> tuple[BoardService, FakeSource]:
    esgg = {**WORLD[0], **{"ident": "ESGG", "latitude": 57.6628, "longitude": 12.2798}}
    scripted: dict[str, Any] = {
        "upstream_status": STATUS,
        "airspace_snapshot": inbound_layer(count),
        "aircraft_track": TRACK_PAYLOAD,
    }
    source = FakeSource(**{**scripted, **answers})
    return BoardService(source, [esgg, *WORLD[1:]]), source


async def test_the_selection_carries_what_approaches_the_selected_airport() -> None:
    service, _ = service_for(5)

    layer = await service.airspace("world", service.selection("ESGG", cap=50, airline="NOZ"))
    without = service.reselect("world", service.selection(None))

    found = layer["selection"]["inbound"]
    assert found["airport"] == "ESGG" and found["approaching"] == 5
    # The airline filter narrows the draw, not what approaches the airport.
    assert layer["selection"]["shown"] == 0
    assert without["selection"]["inbound"] is None


async def test_the_arrival_board_is_asked_once_and_then_matched_for_free() -> None:
    board = {
        "ok": True,
        "window": {"minutes": 60},
        "movements": {"arrival": [{"callsign": "SAS10"}, {"callsign": "SAS11"}]},
        "bookkeeping": {"credits_spent": 4},
    }
    service, source = service_for(3, airport_movements=board)
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.arrival_board("esgg")
    calls = len(source.calls)
    matched = service.reselect("world", service.selection("ESGG"))["selection"]["inbound"]

    assert source.calls[-1] == (
        "airport_movements",
        {"airport": "ESGG", "direction": "arrival", "window_minutes": 60},
    )
    assert (answer["state"], answer["credits_spent"]) == ("read", 4)
    assert len(source.calls) == calls, "matching the held board asks nothing"
    assert matched["text"] == "2 on the arrival board · 1 more approaching · 3 within 60 nm"


async def test_an_unread_board_matches_nothing_and_costs_what_it_reported() -> None:
    service, _ = service_for(1, airport_movements={"ok": False, "error": {"code": "x"}})

    answer = await service.arrival_board("ESGG")

    assert answer["state"] == "unavailable" and answer["credits_spent"] == 0
    with pytest.raises(UnknownBoardItem):
        await service.arrival_board("XXXX")


async def test_observed_tracks_are_bought_for_the_drawn_set_at_the_shown_price() -> None:
    service, source = service_for(3)
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.inbound_tracks("world", "ESGG", 3)

    tracked = [args["icao24"] for tool, args in source.calls if tool == "aircraft_track"]
    assert tracked == ["a00000", "a00001", "a00002"]
    assert all(
        args["estimate_arrival"] is False for t, args in source.calls if t == "aircraft_track"
    )
    assert answer["refused"] is None
    assert [track["state"] for track in answer["tracks"]] == ["observed"] * 3
    assert len(answer["tracks"][0]["waypoints"]) == 11
    assert answer["text"] == "3 observed tracks drawn, 102 credits spent."


async def test_too_few_credits_is_a_refusal_in_the_answer_and_nothing_is_asked() -> None:
    status = {**STATUS, "budget": {"credits_spent_this_hour": 389, "local_budget_per_hour": 400}}
    service, source = service_for(8, upstream_status=status)
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.inbound_tracks("world", "ESGG", 8)

    assert answer["refused"] == "Not enough credits for 8 tracks (32 needed, 11 remaining)."
    assert answer["text"] == answer["refused"]
    assert answer["tracks"] == [] and answer["credits_spent"] == 0
    assert "aircraft_track" not in [tool for tool, _ in source.calls]


async def test_a_price_the_reader_did_not_see_is_not_paid() -> None:
    service, source = service_for(4)
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.inbound_tracks("world", "ESGG", 3)

    assert answer["refused"] == (
        "The approaching set changed since the price was shown: it is now 4 aircraft, "
        "16 credits. Nothing was spent."
    )
    assert "aircraft_track" not in [tool for tool, _ in source.calls]


async def test_a_guard_that_stops_midway_is_said_and_the_rest_stay_projected() -> None:
    answers = iter(
        [
            TRACK_PAYLOAD,
            {"ok": False, "error": {"code": "local_budget_exhausted", "message": "No."}},
        ]
    )
    service, _ = service_for(3, aircraft_track=lambda _args: next(answers))
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.inbound_tracks("world", "ESGG", 3)

    assert len(answer["tracks"]) == 1
    assert answer["text"] == (
        "1 observed track drawn, 34 credits spent. The credit guard stopped after 1 of 3 "
        "tracks: No. The other 2 keep their projected, not observed line."
    )


async def test_a_track_opensky_does_not_hold_keeps_its_projection_and_says_so() -> None:
    empty = {**TRACK_PAYLOAD, "transit": {**TRACK_PAYLOAD["transit"], "waypoints": []}}
    service, _ = service_for(1, aircraft_track=empty)
    await service.airspace("world", service.selection("ESGG"))

    answer = await service.inbound_tracks("world", "ESGG", 1)

    assert answer["tracks"][0]["state"] == "none"
    assert answer["text"] == (
        "0 observed tracks drawn, 34 credits spent; OpenSky held no track for 1, which keep "
        "their projected, not observed line."
    )


async def test_tracks_need_a_held_layer_and_a_listed_airport_and_region() -> None:
    service, _ = service_for(1)

    held_none = await service.inbound_tracks("world", "ESGG", 1)

    assert held_none["refused"] == inbound.NO_LAYER
    with pytest.raises(UnknownBoardItem):
        await service.inbound_tracks("mars", "ESGG", 1)
    with pytest.raises(UnknownBoardItem):
        await service.inbound_tracks("world", "XXXX", 1)


async def test_the_priced_routes_are_posts_and_take_only_a_bounded_count() -> None:
    service, source = service_for(2, airport_movements={"ok": True, "movements": {"arrival": []}})
    app = create_app(service, Gate(None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        await http.get("/api/traffic/world?airport=ESGG")
        as_get = await http.get("/api/traffic/world/inbound/ESGG/tracks?count=2")
        too_many = await http.post("/api/traffic/world/inbound/ESGG/tracks?count=13")
        bought = await http.post("/api/traffic/world/inbound/ESGG/tracks?count=2")
        board = await http.post("/api/airport/ESGG/arrivals")

    assert as_get.status_code == 405
    assert too_many.status_code == 422
    assert bought.json()["text"] == "2 observed tracks drawn, 68 credits spent."
    assert board.json()["state"] == "read"
    assert [t for t, _ in source.calls].count("aircraft_track") == 2


# --- the page: four ways out of a selection ------------------------------------------------
def body(name: str) -> str:
    found = re.search(rf"\n  (?:async )?function {name}\(.*?\n  \}}\n", SCRIPT, re.S)
    assert found is not None, name
    return found.group()


def test_a_click_on_empty_map_clears_the_selection() -> None:
    """The pointer-up handler is the canvas' click; a click on nothing must reach the clear."""
    handler = body("endDrag")

    assert 'else if (!pick) clearSelection("empty map");' in handler
    assert handler.index("if (drag && !drag.moved)") < handler.index("clearSelection(")


def test_escape_releases_follow_first_then_clears() -> None:
    step = body("escape")

    assert "if (state.follow) {" in step
    follow, clear = step.index("stopFollow("), step.index('clearSelection("Escape")')
    assert follow < step.index("return;") < clear
    assert "Press Escape again to clear the selection." in step
    assert re.search(r'event\.key === "Escape" && !event\.defaultPrevented\) escape\(\)', SCRIPT)
    # The old handler did nothing unless following.
    assert 'event.key === "Escape" && state.follow' not in SCRIPT


def test_selecting_the_selected_aircraft_again_toggles_it_off() -> None:
    handler = body("endDrag")

    toggle = 'pick.kind === "aircraft" && pick.item.icao24 === state.focus'
    assert toggle in handler
    assert handler.index(toggle) < handler.index('clearSelection("selected again")')
    assert handler.index('clearSelection("selected again")') < handler.index(
        "focusOn(pick.item.icao24)"
    )


def test_selecting_another_aircraft_replaces_the_whole_selection() -> None:
    focus = body("focusOn")

    for reset in (
        "state.focusData = null;",
        "state.track = null;",
        "state.follow = false;",
        'state.followNote = "";',
        "state.followHeard = null;",
        "renderAircraftPanel();",
    ):
        assert reset in focus, reset
    assert "Selection moved to" in focus


def test_every_exit_goes_through_one_clear_that_says_which() -> None:
    clear = body("clearSelection")

    assert "focusOn(null);" in clear and "say(`Selection cleared (${how})" in clear
    assert re.search(
        r'\$\("a-clear"\)\.addEventListener\("click", \(\) => clearSelection\(', SCRIPT
    )
    assert 'id="map-status"' in (STATIC / "index.html").read_text(encoding="utf-8")


def test_a_cleared_selection_leaves_no_highlight_panel_or_late_answer() -> None:
    panel = body("renderAircraftPanel")
    draw = body("draw")
    focus = body("applyFocus")

    assert "panel.hidden = !state.focus;" in panel
    # Brackets, trail and transit are drawn only while something is selected (and the
    # aircraft layer is on: tests/test_airport_layers.py).
    assert "state.focus) { drawTrail(state.focus); drawFocused(); }" in draw
    assert "state.focus) drawTransit(path);" in draw
    # A poll sent before the change answers for the old aircraft and is not painted.
    assert "answer && answer.icao24 === state.focus ? answer : null" in focus


def test_a_selection_survives_a_poll_but_not_a_region_change() -> None:
    poll = body("refreshTraffic")
    region = re.search(r'\$\("region"\)\.addEventListener\("change".*?\n  \}\);', SCRIPT, re.S)

    assert "focusOn" not in poll and "clearSelection" not in poll
    assert 'if (state.focus) params.set("focus", state.focus);' in body("selectionParams")
    assert region is not None
    assert 'clearSelection("region changed");' in region.group()


# --- the page: projected lines -------------------------------------------------------------
def test_the_projected_line_is_dashed_thinner_and_named_projected() -> None:
    lines = body("drawInbound")
    legend = body("inboundLegend")
    widths = {
        name: float(value)
        for name, value in re.findall(r"const (PROJECTED_WIDTH|OBSERVED_WIDTH) = ([\d.]+);", SCRIPT)
    }

    assert "context.setLineDash(PROJECTED_DASH);" in lines
    assert re.search(r"const PROJECTED_DASH = \[\d", SCRIPT)
    assert widths["PROJECTED_WIDTH"] < widths["OBSERVED_WIDTH"]
    assert "dashed is projected, not observed" in legend
    assert "legend.textContent = inbound ? inboundLegend()" in body("renderInbound")
    # The observed transit is solid now: a dash means projected on this board.
    assert "context.setLineDash([]);" in body("drawTransit")


def test_approaching_aircraft_rise_above_the_others_and_the_lit_one_thickens() -> None:
    draw = body("draw")
    lines = body("drawInbound")

    assert "!approaching.has(aircraft.icao24)" in draw
    assert draw.index("drawn.forEach") < draw.index("drawInbound(path);")
    assert "context.arc(point.at[0], point.at[1], INBOUND_RING_PX" in lines
    assert "const extra = lit === aircraft.icao24 ? LIT_EXTRA : 0;" in lines
    assert "context.globalAlpha = lit && !extra ? FADED : 0.85;" in lines


def test_a_refused_purchase_is_printed_not_swallowed() -> None:
    handler = re.search(r'\$\("i-tracks"\)\.addEventListener\("click".*?\n  \}\);', SCRIPT, re.S)

    assert handler is not None
    assert "note.textContent = answer.text;" in handler.group()
    assert '{ method: "POST" }' in handler.group()
    assert "?count=${inbound.price.count}" in handler.group()


def test_convergence_holds_its_own_radius_and_the_row_words_its_gaps() -> None:
    far = {**approaching("aaa001", 180, 61), "distance_nm": 61.0, "bearing_to_airport_deg": 0}
    unnamed = approaching("aaa002", 180, 20, callsign="QQQ1", altitude_m=None)

    row = inbound_view([unnamed], ESGG, None)["aircraft"][0]

    assert not convergent(far)
    assert row["airline_name"] is None
    assert row["text"].startswith("QQQ1 · 20.0 nm · altitude not reported · ")

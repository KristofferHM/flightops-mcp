"""An airport selection has a way out, and the traffic layer says how old it is.

Two reports from a phone: an airport could not be deselected, and flights did not seem to
update while one was selected. The first was a missing feature: nothing set the selection
back. The second was measured (five minutes in a browser, every `/api/traffic` request
logged) and was not a fault in the page: at the default ceiling the layer refreshes every
240 s, and the page did not say so. The browser half (the four exits pressed for real, late
answers held back by a route, and the countdown run on a fake clock) is in `scripts/a11y.mjs`.
"""

from __future__ import annotations

import re
from pathlib import Path

from dashboard import service
from dashboard.service import TRAFFIC_COST, TRAFFIC_MIN_INTERVAL_S, TrafficSpend
from flightops_mcp.config import Settings

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "dashboard" / "static"
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
GATE = (ROOT / "scripts" / "a11y.mjs").read_text(encoding="utf-8")


def body(name: str) -> str:
    found = re.search(rf"\n  (?:async )?function {name}\(.*?\n  \}}\n", SCRIPT, re.S)
    assert found is not None, name
    return found.group()


def handler(element: str, event: str) -> str:
    found = re.search(rf'\$\("{element}"\)\.addEventListener\("{event}".*?\n  \}}\);', SCRIPT, re.S)
    assert found is not None, element
    return found.group()


# --- the four exits, one clear ------------------------------------------------------------
def test_every_airport_exit_ends_in_one_clear_that_says_which() -> None:
    clear = body("clearAirport")

    assert "if (!state.selected) return;" in clear
    assert "state.selected = null;" in clear
    assert "say(`Airport cleared (${how}): ${icao}.`);" in clear
    # Exactly the four exits call it, each naming itself.
    named = re.findall(r'clearAirport\("([^"]+)"\)', SCRIPT)
    assert sorted(named) == ["selected again", "the Close button"]
    assert re.findall(r'letGo\("([^"]+)"\)', SCRIPT) == ["Escape", "empty map"]
    assert "else clearAirport(how);" in body("letGo")


def test_a_click_on_empty_map_lets_go_innermost_first() -> None:
    drag = body("endDrag")
    go = body("letGo")

    assert 'else if (!pick) letGo("empty map");' in drag
    assert drag.index("if (drag && !drag.moved)") < drag.index("letGo(")
    # An aircraft, when one is focused, goes before the airport it may be bound for.
    assert go.index("if (state.focus) clearSelection(how);") < go.index("clearAirport(how)")


def test_escape_releases_the_follow_then_the_aircraft_then_the_airport() -> None:
    step = body("escape")

    assert step.index("stopFollow(") < step.index("return;") < step.index('letGo("Escape")')


def test_the_selected_airport_pressed_again_is_a_toggle() -> None:
    choose = body("select")

    toggle = 'if (icao === state.selected) { clearAirport("selected again"); return; }'
    assert toggle in choose
    # Before anything of the new selection is set up.
    assert choose.index(toggle) < choose.index("state.selected = icao;")
    # The strip and the map both reach it: both call `select`.
    assert 'strip.addEventListener("click", () => select(airport.icao));' in SCRIPT
    assert 'if (pick && pick.kind === "airport") select(pick.item.icao);' in body("endDrag")


def test_the_panel_has_a_close_a_thumb_can_reach() -> None:
    assert re.search(r'<button id="d-close" class="quiet" type="button"[^>]*>Close</button>', PAGE)
    assert PAGE.index('id="d-close"') > PAGE.index('<div id="detail" hidden>')
    close = '$("d-close").addEventListener("click", () => clearAirport("the Close button"));'
    assert close in SCRIPT
    sheet = (STATIC / "board.css").read_text(encoding="utf-8")
    assert "#d-close { min-height: 44px; min-width: 44px;" in sheet


# --- what a clear leaves ------------------------------------------------------------------
def test_a_clear_leaves_no_panel_strip_inbound_or_approach_line() -> None:
    clear = body("clearAirport")
    forget = body("forgetAirport")

    for gone in (
        "state.inboundTracks.clear();",
        "renderInbound(null);",
        '$("triage-result").hidden = true;',
        "state.detail = null;",
    ):
        assert gone in forget, gone
    assert "forgetAirport();" in clear and "forgetAirport();" in body("select")
    assert '$("detail").hidden = true;' in clear and '$("detail-empty").hidden = false;' in clear
    assert "markStrips();" in clear
    # The traffic layer is read again without the airport: free, from the layer held.
    assert "refreshSelection();" in clear
    assert 'if (state.selected) params.set("airport", state.selected);' in body("selectionParams")
    # Approach lines and range rings are drawn only from the selected airport.
    assert "if (!state.inbound || !airport) return;" in body("drawInbound")


def test_a_late_answer_never_repaints_after_a_clear() -> None:
    detail = body("refreshDetail")
    triage = handler("triage", "click")

    # Success and failure alike: a failure is an answer too.
    assert detail.count("if (icao !== state.selected) return;") == 2
    assert triage.count("if (icao !== state.selected) return;") == 2
    for name in ("i-tracks", "i-board"):
        assert handler(name, "click").count("if (icao !== state.selected") == 2
    # Inbound drawn only for the airport selected now.
    assert "inbound && inbound.airport === state.selected ? inbound : null" in body("applyLayer")
    # A traffic answer asked under other choices (an airport since let go) does not paint its
    # selection; the current choices are read from the layer it left on the server.
    traffic = body("refreshTraffic")
    assert "const asked = selectionParams();" in traffic
    applied = r"if \(asked === selectionParams\(\)\) \{.*?applyLayer\(layer\);"
    assert re.search(applied, traffic, re.S)
    assert "} else {" in traffic and traffic.count("refreshSelection();") == 1
    # And a free re-selection that read the older layer cannot overwrite the newer one.
    assert "state.selectionRequest += 1;" in traffic
    assert "if (request !== state.selectionRequest" in body("refreshSelection")


def test_the_gate_presses_every_exit_and_holds_back_every_late_answer() -> None:
    for how in ("Escape", "selected again", "the Close button", "empty map"):
        assert f'exit("{how}"' in GATE, how
    for late in ("a failed detail", "a failed triage", "detail repainted", "a traffic answer"):
        assert late in GATE, late
    assert "await exerciseAirportExits(page, size);" in GATE
    assert "await checkLateAnswers(page, size);" in GATE


# --- the traffic timeline: the finding ----------------------------------------------------
def test_at_the_default_ceiling_the_layer_refreshes_every_four_minutes() -> None:
    """The measured cause: not a stalled chain, an interval twice the page's floor."""
    default = Settings.model_fields["dashboard_traffic_credits_per_hour"].default
    spend = TrafficSpend(default, clock=lambda: 0.0)

    assert (default, TRAFFIC_COST) == (60, 4)
    assert spend.interval_s() == 240 > TRAFFIC_MIN_INTERVAL_S
    assert spend.view()["interval_s"] == 240
    # The page's floor is the server's, so the countdown never promises sooner than allowed.
    assert f"const TRAFFIC_POLL_MS = {TRAFFIC_MIN_INTERVAL_S * 1000:_};" in SCRIPT
    assert service.TRAFFIC_MIN_INTERVAL_S == 120


def test_the_toggle_says_the_interval_first() -> None:
    cost = body("renderTrafficCost")

    assert cost.index("Traffic refreshes about every ${every(") < cost.index("This session")
    every = body("every")
    assert 'return minutes === 1 ? "minute" : `${minutes} minutes`;' in every


def test_the_traffic_line_says_when_it_was_heard_and_when_it_will_ask() -> None:
    line = body("trafficClock")

    assert "heard ${ago(heard.age + since)} ago" in line
    assert "next refresh in ${clock((due.traffic - now) / 1000)}" in line
    assert '" · "' in line
    # The countdown is the armed timer's own, jitter included.
    assert "due[name] = performance.now() + wait * jitter;" in body("schedule")
    # The age is the upstream's two clocks, then this page's monotonic clock: no Date.
    assert "layer.upstream_epoch - layer.snapshot_epoch" in body("refreshTraffic")
    assert "Date.now()" not in SCRIPT and "new Date(" not in SCRIPT
    # Ticking once a second, so kept out of the live note a screen reader announces.
    assert '<span id="traffic-note" role="status"></span> <span id="traffic-clock"></span>' in PAGE
    assert "tickTrafficClock();" in body("startPolling")


def test_after_a_selection_exactly_one_traffic_chain_runs() -> None:
    """The first suspect, ruled out by measurement: `select` touches the detail chain only."""
    choose = body("select")

    assert 'schedule("detail", refreshDetail, DETAIL_POLL_MS);' in choose
    assert '"traffic"' not in choose and "refreshTraffic" not in choose
    # `schedule` clears the timer it replaces, so a chain is never doubled.
    sched = body("schedule")
    assert sched.index("clearTimeout(timers[name]);") < sched.index("timers[name] = setTimeout(")
    assert "checkTrafficTimeline(browser, servers, passcode);" in GATE
    assert "refreshes when the countdown ended, not 1" in GATE

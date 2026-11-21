"""Quiet panels: a number or a verdict stays on screen, a reason moves behind a `?`.

The server words both forms. The `short` form is what the page prints; the whole sentence is
kept, unchanged, in the field it always had, and the page puts it in the section's tooltip.
The browser half (focus, hover, Escape, tap, no layout shift, no overlapping panels at
1280x720 and 390x844) is in `scripts/a11y.mjs`; the static half and the payloads are here.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from dashboard import aircraft, board, inbound, operator
from dashboard.service import COVERAGE_NOTE, COVERAGE_SHORT, View, _tracks_answer, traffic_default
from tests.test_aircraft_layer import TRACK_PAYLOAD, world_service

ROOT = Path(__file__).parent.parent
STATIC = ROOT / "dashboard" / "static"
PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")
SHEET = (STATIC / "board.css").read_text(encoding="utf-8")
GATE = (ROOT / "scripts" / "a11y.mjs").read_text(encoding="utf-8")

# Each short field, and the field beside it that keeps the whole sentence.
WHOLE = {
    "short": "text",
    "shown_short": "shown_text",
    "coverage_short": "coverage",
    "reason_short": "reason",
    "classification_short": "classification_text",
    "dropped_short": "dropped",
    "fallback_short": "fallback",
}


def words(text: str) -> list[str]:
    return [w for w in (re.sub(r"[^\w→–°±/]", "", token) for token in text.split()) if w]


def taken_out(short: str, whole: str) -> bool:
    """`short` is `whole` with words removed, none added or changed: a subsequence of words."""
    rest = iter(words(whole))
    return all(word in rest for word in words(short))


def pairs(payload: Any) -> list[tuple[str, str]]:
    """Every (short, whole) pair in a payload, at any depth."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in WHOLE and isinstance(value, str):
                found.append((value, payload[WHOLE[key]]))
            found.extend(pairs(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(pairs(item))
    return found


# --- the payloads: the short form only takes words out -----------------------------------
@pytest.mark.parametrize("unknown", [0, 1, 1888])
def test_the_unknown_toggle_keeps_its_count_and_moves_its_reason(unknown: int) -> None:
    assert board.hidden_note(unknown).startswith(board.hidden_short(unknown))
    assert board.shown_note(unknown).startswith(board.shown_short(unknown))
    assert board.hidden_short(1888) == "1,888 hidden"
    assert board.shown_short(1888) == "1,888 without a report"
    assert board.hidden_short(0) == "none hidden"


@pytest.mark.parametrize(
    ("args", "short"),
    [
        ((3270, 3270, 600, 600, 6), "Weather showing 600 of 3,270"),
        ((3270, 41, 41, 41, 1), "Weather showing all 41 in view"),
    ],
)
def test_the_view_line_keeps_how_many_and_moves_which_and_the_cost(
    args: tuple[int, int, int, int, int], short: str
) -> None:
    region_total, in_view, asked, looked_up, calls = args
    whole = board.viewport_note(region_total, in_view, asked, looked_up, calls)

    assert board.viewport_short(in_view, asked) == short
    assert whole.startswith(short)
    assert "lookups in" not in short


@pytest.mark.parametrize("mode", ["client_credentials", "anonymous", None])
def test_the_traffic_default_keeps_its_verdict(mode: str | None) -> None:
    default = traffic_default(mode)

    assert default["reason"].startswith(default["reason_short"])
    assert len(words(default["reason_short"])) <= 8


def test_the_constant_short_forms_open_their_sentences() -> None:
    assert COVERAGE_NOTE.startswith(COVERAGE_SHORT)
    assert operator.GRADE_TEXT.startswith(operator.GRADE_SHORT)
    assert inbound.NO_LAYER.startswith(inbound.NO_LAYER_SHORT)
    assert taken_out(aircraft.NO_AIRPORT_SORT_SHORT, aircraft.NO_AIRPORT_SORT)
    assert aircraft.dropped_short(200, 412) == "showing 200 of 412"
    assert aircraft.dropped_short(200, 200) is None
    # The page keeps its own copy of the inbound line; it must be the server's.
    assert f'const INBOUND_OFF_SHORT = "{inbound.NO_LAYER_SHORT}";' in SCRIPT


def test_the_three_claims_keep_their_values_and_move_their_reasons() -> None:
    lines = {row["label"]: row for row in aircraft.track_view(TRACK_PAYLOAD)["lines"]}

    transit, arrival, destination = (
        lines["Transit"],
        lines["Estimated arrival"],
        lines["Destination"],
    )
    assert transit["short"].startswith("observed: 11 waypoints, ")
    assert "nothing drawn past the last" not in transit["short"]
    assert "nothing drawn past the last" in transit["text"]
    # The last listed flight is a value: it stays on screen. Its basis moves.
    assert "OMAD → EGLL" in arrival["short"]
    assert "not a schedule" not in arrival["short"] and "not a schedule" in arrival["text"]
    assert destination["short"] == "No source"
    assert destination["text"] == "No source. A transponder broadcasts position."
    for row in lines.values():
        assert taken_out(row["short"], row["text"]), row


@pytest.mark.parametrize(
    ("arrival", "short"),
    [
        ({"state": "estimated", "airport": "ENGM"}, "ENGM"),
        ({"state": "not_requested"}, "not requested"),
        ({"state": "unavailable", "error": "credentials_missing"}, None),
        ({"state": "not_estimated"}, None),
        ({"state": "no_record"}, "no record for this flight yet"),
    ],
)
def test_an_estimate_keeps_its_verdict(arrival: dict[str, Any], short: str | None) -> None:
    whole = aircraft.arrival_text(arrival)

    # None: a reason that is the answer stays whole ("Weather unavailable (code)" is not clutter).
    assert aircraft.arrival_short(arrival) == (whole if short is None else short)
    assert taken_out(aircraft.arrival_short(arrival), whole)


def test_a_failed_track_says_not_known_and_keeps_its_code() -> None:
    lines = aircraft.track_view({"ok": False, "error": {"code": "upstream_unavailable"}})["lines"]

    assert [row["short"] for row in lines] == [
        "unavailable (upstream_unavailable)",
        "not known",
        "No source",
    ]


@pytest.mark.parametrize(
    ("tracks", "spent", "refused", "short"),
    [
        (["observed", "observed", "none"], 12, None, "2 observed tracks drawn, 12 credits spent; "),
        ([], 0, "Not enough credits for 3 tracks (12 needed, 4 remaining).", "Not enough"),
        (["observed"], 4, "Refused.", "1 observed track drawn, 4 credits spent. Refused."),
    ],
)
def test_a_track_answer_keeps_its_counts_and_any_refusal(
    tracks: list[str], spent: int, refused: str | None, short: str
) -> None:
    answer = _tracks_answer(
        "ESGG", [{"state": state} for state in tracks], len(tracks), spent, refused
    )

    assert answer["short"].startswith(short)
    assert taken_out(answer["short"], answer["text"])
    if "none" in tracks:
        assert answer["short"].endswith("OpenSky held no track for 1.")
        assert "projected, not observed" in answer["text"]


def test_the_arrival_board_keeps_its_count() -> None:
    read = inbound.board_view(
        {"ok": True, "movements": {"arrival": [{"callsign": "SAS1"}]}, "window": {"minutes": 60}}
    )
    failed = inbound.board_view({"ok": False, "error": {"message": "rate limited"}})

    assert read["short"] == "1 on OpenSky's arrival list in the last 60 min."
    assert read["text"].startswith(read["short"])
    assert failed["short"] == failed["text"] == "Arrival board unavailable: rate limited"


async def test_every_short_form_in_the_payloads_only_takes_words_out() -> None:
    service, _ = world_service()
    snapshot = await service.snapshot("world", view=View(20.0, 10.0, 180.0))
    layer = await service.airspace("world", service.selection(None, cap=50))
    found = pairs(snapshot) + pairs(layer)

    # The walk reached the toggle, the coverage line, the traffic reason and the chips.
    shorts = {short for short, _ in found}
    assert {snapshot["coverage_short"], snapshot["hidden"]["short"]} <= shorts
    assert snapshot["traffic"]["reason_short"] in shorts
    assert layer["operator_classes"]["classification_short"] in shorts
    assert len(found) >= 6, found
    for short, whole in found:
        assert short and len(short) <= len(whole)
        assert taken_out(short, whole), (short, whole)


async def test_a_short_form_is_added_beside_the_sentence_never_instead_of_it() -> None:
    """The tooltip changes no payload field: every sentence is still where it was."""
    service, _ = world_service()
    snapshot = await service.snapshot("world", view=View(20.0, 10.0, 180.0))
    hidden = snapshot["hidden"]
    viewport = snapshot["viewport"]

    assert snapshot["coverage"] == COVERAGE_NOTE
    assert hidden["text"] == board.hidden_note(hidden["unknown"])
    assert hidden["shown_text"] == board.shown_note(hidden["unknown"])
    assert viewport["text"] == board.viewport_note(
        viewport["region_total"],
        viewport["in_view"],
        viewport["asked"],
        viewport["looked_up"],
        viewport["calls"],
    )
    assert snapshot["traffic"]["reason"] == traffic_default("client_credentials")["reason"]
    # The banner is counted from the reports, and no short form reaches it.
    assert "short" not in snapshot["banner"]


# --- the page: a real button, described, one per section ---------------------------------
class Tree(HTMLParser):
    """Each element's tag, attributes, parent index and text, in document order."""

    VOID = frozenset({"input", "meta", "link", "br", "img", "hr"})

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[dict[str, Any]] = []
        self.stack: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.stack[-1] if self.stack else None
        self.nodes.append({"tag": tag, "attrs": dict(attrs), "parent": parent, "text": ""})
        if tag not in self.VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        for index in self.stack:
            self.nodes[index]["text"] += data


def tree() -> Tree:
    parsed = Tree()
    parsed.feed(PAGE)
    return parsed


def classes(node: dict[str, Any]) -> list[str]:
    return (node["attrs"].get("class") or "").split()


def test_every_question_mark_is_a_button_described_by_a_tooltip() -> None:
    nodes = tree().nodes
    by_id = {node["attrs"]["id"]: node for node in nodes if node["attrs"].get("id")}
    buttons = [node for node in nodes if "hint-button" in classes(node)]

    assert len(buttons) >= 10
    for button in buttons:
        attrs = button["attrs"]
        assert button["tag"] == "button" and attrs.get("type") == "button", attrs
        assert attrs.get("aria-label", "").startswith("About "), attrs
        tip = by_id[attrs["aria-describedby"]]
        assert tip["attrs"].get("role") == "tooltip"
        assert "hint-text" in classes(tip)
        # The tooltip sits in the same `.hint` as its button, so hovering onto it keeps it open.
        assert tip["parent"] == button["parent"]


def test_one_question_mark_per_section() -> None:
    nodes = tree().nodes
    per_parent: dict[int, int] = {}
    for node in nodes:
        if "hint" in classes(node):
            per_parent[node["parent"]] = per_parent.get(node["parent"], 0) + 1

    assert per_parent and max(per_parent.values()) == 1


def test_no_explanation_hides_in_a_title_attribute() -> None:
    """A `title` cannot be reached by keyboard or touch, and cannot be styled."""
    assert not [node for node in tree().nodes if "title" in node["attrs"]]
    assert ".title =" not in SCRIPT


def test_the_legend_sentences_moved_into_its_tooltip_word_for_word() -> None:
    nodes = tree().nodes
    legend_why = next(node for node in nodes if node["attrs"].get("id") == "legend-why")
    text = " ".join(legend_why["text"].split())

    assert "VFR ceiling over 3000 ft and visibility over 5 mi" in text
    assert "Unknown: no report heard, not good news." in text
    assert "A numbered marker is a group of airports" in text
    # The verdict stays on screen.
    assert "Markers show weather only, not airport status." in PAGE.split('id="legend-why"')[0]


@pytest.mark.parametrize(
    "sentence",
    [
        "the cap and the filters on the right decide which.",
        ", on the markers and the groups.",
        "Press Escape again to clear the selection.",
        "It reads the traffic layer ",
        "No track was drawn; the lines stay projected.",
        "The arrival board was not read.",
        "Resets when the board restarts.",
    ],
)
def test_the_page_still_holds_each_sentence_it_moved(sentence: str) -> None:
    assert sentence in SCRIPT


def test_a_closed_explanation_is_clipped_not_removed() -> None:
    """`display: none` would take it out of reach of aria-describedby in some readers."""
    closed = re.search(r"\.hint-text \{(.*?)\}", SHEET, re.S)
    opened = re.search(r"\.hint-text\[data-open\] \{(.*?)\}", SHEET, re.S)

    assert closed is not None and opened is not None
    assert "clip-path: inset(50%)" in closed.group(1)
    assert "display" not in closed.group(1) and "visibility" not in closed.group(1)
    # Fixed to the viewport, so opening it pushes nothing.
    assert "position: fixed" in closed.group(1)
    assert "clip-path: none" in opened.group(1)


def test_escape_closes_an_explanation_before_it_releases_a_follow() -> None:
    assert re.search(
        r'document\.addEventListener\("keydown", \(event\) => \{\n'
        r'    if \(event\.key !== "Escape"\).*?event\.preventDefault\(\);\n  \}, true\);',
        SCRIPT,
        re.S,
    )
    assert 'if (event.key === "Escape" && !event.defaultPrevented) escape();' in SCRIPT


def test_the_gate_checks_both_poster_and_phone_sizes() -> None:
    assert "{ width: 1280, height: 720 }" in GATE
    assert "{ width: 390, height: 844 }" in GATE
    for check in ("checkLayout(page, where)", "checkHints(page, where)", "exerciseHints("):
        assert check in GATE

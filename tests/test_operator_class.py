"""Who is flying, from the callsign alone: the classifier, its evidence, the chips and the filter.

The callsigns are a live sample around ESGG on 2026-11-20. This board's source sends no
aircraft type, so each is classed as the callsign alone allows, and every answer is graded
`callsign only`.
"""

from __future__ import annotations

import csv
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

from dashboard import aircraft, board, operator
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.inbound import DRAWN_MAX, inbound_view
from dashboard.operator import AIRLINE, GENERAL, MILITARY, UNKNOWN, classify, registration
from tests.test_aircraft_layer import LAYER, NOW, world_service
from tests.test_inbound import ESGG, approaching

STATIC = Path(__file__).parent.parent / "dashboard" / "static"
SCRIPT = (STATIC / "board.js").read_text(encoding="utf-8")
COLUMNS = [
    "icao24",
    "callsign",
    "latitude",
    "longitude",
    "baro_altitude_m",
    "on_ground",
    "velocity_ms",
    "true_track_deg",
    "last_contact",
]
LIVE = [
    ("780a3b", "CES213"),
    ("4ac9e5", "SAS1396"),
    ("4ab4f1", "SEMOY"),
    ("45ac8e", "OYKEJ"),
    ("4b1a2c", "DOC05"),
    ("4ab001", "TWR"),
]


def snapshot(rows: list[tuple[str, str]]) -> dict[str, Any]:
    """An airspace_snapshot payload in OpenSky's columns: no type, no category."""
    return {
        "ok": True,
        "columns": COLUMNS,
        "rows": [
            [icao24, call, 57.7, 12.3, 3000.0, False, 120.0, 90.0, NOW - 5] for icao24, call in rows
        ],
        "snapshot_time_epoch": NOW,
        "upstream_time_epoch": NOW,
        "snapshot_current": True,
    }


# --- the live sample -----------------------------------------------------------------------
def test_the_live_sample_classifies_as_the_callsign_allows() -> None:
    view = board.airspace_view(snapshot(LIVE))

    got = {a["callsign"]: (a["operator_class"], a["class_evidence"]) for a in view["aircraft"]}
    assert got == {
        "CES213": (AIRLINE, "callsign CES213"),
        "SAS1396": (AIRLINE, "callsign SAS1396"),
        "SEMOY": (GENERAL, "callsign SE-MOY"),
        "OYKEJ": (GENERAL, "callsign OY-KEJ"),
        # A helicopter in fact, but DOC is in no table: unknown, never guessed.
        "DOC05": (UNKNOWN, "nothing matched"),
        # A tower transmitter in fact. Without a type it cannot be told from an aircraft, so it
        # is not left out; it is unknown, which claims nothing.
        "TWR": (UNKNOWN, "nothing matched"),
    }
    assert view["operator_classes"]["classification"] == "callsign only"


# --- the rules -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("callsign", "expected"),
    [
        ("SAS1396", (AIRLINE, "callsign SAS1396")),
        (" sas1396 ", (AIRLINE, "callsign SAS1396")),
        # NetJets is airline-shaped. From the callsign alone it reads as an airline, and the
        # grade's text says that this is what callsign-only cannot tell apart.
        ("NJE123", (AIRLINE, "callsign NJE123")),
        ("RCH4521", (MILITARY, "callsign RCH4521")),
        ("GAF684", (MILITARY, "callsign GAF684")),
        ("SEMOY", (GENERAL, "callsign SE-MOY")),
        ("N12345", (GENERAL, "callsign N12345")),
        # An unknown designator stays unknown: never promoted because it sounds like something.
        ("DOC05", (UNKNOWN, "nothing matched")),
        ("QQQ123", (UNKNOWN, "nothing matched")),
        # A designator with no flight number is not a flight.
        ("SAS", (UNKNOWN, "nothing matched")),
        ("RCH", (UNKNOWN, "nothing matched")),
        ("", (UNKNOWN, "nothing matched")),
        (None, (UNKNOWN, "nothing matched")),
        (7, (UNKNOWN, "nothing matched")),
    ],
)
def test_the_callsign_is_the_only_evidence(callsign: object, expected: tuple[str, str]) -> None:
    assert classify(callsign) == expected


def test_every_layer_is_graded_callsign_only() -> None:
    view = board.airspace_view(LAYER)
    traffic = board.traffic_view({"ok": True, "counts": {"returned": 0}, "aircraft": []})

    for classes in (view["operator_classes"], traffic["operator_classes"]):
        assert classes["classification"] == "callsign only"
        assert classes["classification_text"].startswith("Classed by callsign only")
    assert {a["class_evidence"] for a in view["aircraft"]} == {"callsign SAS1"}
    assert not [a for a in view["aircraft"] if {"type_code", "category"} & set(a)]


def test_no_type_path_is_left_on_this_branch() -> None:
    source = inspect.getsource(operator) + inspect.getsource(board)
    assert "type_code" not in source and "TYPE_TABLE" not in source
    assert not (Path(operator.__file__).parent / "data" / "operator_types.csv").exists()
    assert set(inspect.signature(classify).parameters) == {"callsign"}


@pytest.mark.parametrize(
    ("callsign", "written"),
    [
        ("SEMOY", "SE-MOY"),
        ("oykej", "OY-KEJ"),
        ("GABCD", "G-ABCD"),
        ("DEABC", "D-EABC"),
        ("HBZZZ", "HB-ZZZ"),
        ("CFABC", "C-FABC"),
        ("N1", "N1"),
        ("N123AB", "N123AB"),
        ("SEMOYX", None),
        ("SEMO", None),
        ("N0123", None),
        ("SAS123", None),
        ("QQABC", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_a_registration_is_read_only_in_its_country_s_shape(
    callsign: object, written: str | None
) -> None:
    assert registration(callsign) == written


# --- the table -----------------------------------------------------------------------------
def test_the_designator_table_is_well_formed_and_documented() -> None:
    with operator.DESIGNATOR_TABLE.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    codes = [row["designator"] for row in rows]

    assert list(rows[0]) == ["designator", "class", "note"]
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[A-Z]{3}", code) for code in codes)
    assert {row["class"] for row in rows} == {MILITARY}
    assert all(row["note"].strip() for row in rows)
    # Kept out of the airline table, so they never read as airlines.
    assert not set(codes) & set(aircraft.airline_table())
    readme = " ".join((operator.DATA / "README.md").read_text(encoding="utf-8").split())
    assert "operator_designators.csv" in readme and "only when its class is certain" in readme


# --- the inspector and the glossary ---------------------------------------------------------
def test_the_inspector_shows_the_class_and_its_evidence_next_to_the_airline() -> None:
    entry = board.airspace_view(snapshot([("aaa001", "SEMOY")]))["aircraft"][0]

    rows = [(r["label"], r["text"]) for r in aircraft.aircraft_rows(entry, None)]
    labels = [label for label, _ in rows]
    assert labels.index("Class") == labels.index("Airline") + 1
    assert dict(rows)["Class"] == "general aviation (callsign SE-MOY)"


def test_an_unclassified_entry_says_so() -> None:
    rows = {r["label"]: r["text"] for r in aircraft.aircraft_rows({"icao24": "a"}, None)}
    assert rows["Class"] == "not classified"


def test_the_glossary_gives_each_class_its_one_sentence() -> None:
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert set(operator.GLOSSARY) == set(operator.CLASSES)
    for sentence in operator.GLOSSARY.values():
        assert sentence in page


# --- the chips -----------------------------------------------------------------------------
def test_the_chip_counts_equal_the_classified_totals() -> None:
    view = board.airspace_view(snapshot([*LIVE, ("aaa001", "RRR4920")]))
    classes = view["operator_classes"]

    tally = dict.fromkeys(operator.CLASSES, 0)
    for entry in view["aircraft"]:
        tally[entry["operator_class"]] += 1
    assert classes["counts"] == tally == {AIRLINE: 2, GENERAL: 2, MILITARY: 1, UNKNOWN: 2}
    assert classes["all"] == len(view["aircraft"]) == 7
    assert set(classes["labels"]) == set(operator.CLASSES)


async def test_the_counts_describe_the_region_not_the_drawn_cap() -> None:
    service, _ = world_service()
    layer = await service.airspace("world", service.selection(None, cap=50))

    assert len(layer["aircraft"]) == 50
    assert layer["operator_classes"]["all"] == 300


async def test_filtering_by_class_changes_no_payload_field() -> None:
    """The class filter is a render decision in the page: the server is never told of it."""
    service, _ = world_service()
    await service.airspace("world", service.selection(None))

    first = service.reselect("world", service.selection(None))
    second = service.reselect("world", service.selection(None))
    assert first == second
    assert "operator_classes" in first
    assert all("operator_class" in a for a in first["aircraft"])
    # No route takes it, and the page sends it nowhere.
    routes = {r.path: r for r in create_app(service, Gate(None)).routes}  # type: ignore[attr-defined]
    for path in ("/api/traffic/{region}", "/api/traffic/{region}/selection"):
        params = inspect.signature(routes[path].endpoint).parameters  # type: ignore[attr-defined]
        assert not [name for name in params if "class" in name]
    sent = re.search(r"function selectionParams\(\) \{(.*?)\n  \}", SCRIPT, re.S)
    assert sent and "operatorClass" not in sent.group(1)
    assert not [path for path in re.findall(r"api\(`([^`]*)`", SCRIPT) if "class" in path.lower()]


def test_the_page_filters_the_map_the_globe_the_list_and_the_inbound_together() -> None:
    draw = re.search(r"function draw\(\) \{(.*?)\n  \}", SCRIPT, re.S)
    assert draw and "state.airspace.filter(inClass)" in draw.group(1)
    picks = re.search(r"function renderSelection\(selection\) \{(.*?)\n  \}", SCRIPT, re.S)
    assert picks and "state.airspace.filter(inClass)" in picks.group(1)
    assert "inbound.aircraft.filter(inClass)" in SCRIPT
    drawn_inbound = re.search(r"function drawInbound\(path\) \{(.*?)\n  \}", SCRIPT, re.S)
    assert drawn_inbound and "state.inbound.aircraft" not in drawn_inbound.group(1)


def test_the_filter_is_kept_for_the_session_and_dropped_with_the_region() -> None:
    assert "sessionStorage" in SCRIPT
    wiring = r'\$\("region"\)\.addEventListener\("change", \(\) => \{(.*?)\n  \}\);'
    region = re.search(wiring, SCRIPT, re.S)
    assert region and 'rememberClass("")' in region.group(1)
    assert 'state.operatorClass = ""' in region.group(1)


# --- the inbound list ----------------------------------------------------------------------
def test_the_inbound_class_counts_cover_every_candidate_not_only_the_drawn() -> None:
    """The page's `N more approaching, hidden by the filter` reads these: they must be real."""
    traffic = []
    for n in range(DRAWN_MAX + 6):
        callsign = "SEMOY" if n % 3 == 0 else f"SAS{100 + n}"
        entry = approaching(f"bb{n:04x}", 180, 5 + n)
        found, evidence = classify(callsign)
        entry.update(callsign=callsign, operator_class=found, class_evidence=evidence)
        traffic.append(entry)

    view = inbound_view(traffic, ESGG, None)

    assert view["drawn"] == DRAWN_MAX and view["candidates"] == DRAWN_MAX + 6
    assert view["classes"] == {GENERAL: 6, AIRLINE: 12}
    drawn_general = sum(1 for a in view["aircraft"] if a["operator_class"] == GENERAL)
    assert drawn_general < view["classes"][GENERAL], "the drawn few would undercount it"
    # The page's arithmetic: candidates less the chosen class.
    hidden = re.search(r"function inboundHiddenText\(inbound\) \{(.*?)\n  \}", SCRIPT, re.S)
    assert hidden and "inbound.candidates - kept" in hidden.group(1)
    assert "more approaching, hidden by the filter" in hidden.group(1)

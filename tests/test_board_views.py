"""The exact output of `dashboard/board.py`, written from its mutation survivors.

Whole dictionaries on purpose. `board.js` reads these keys and prints these words, so here the
wording is the behaviour: a changed key blanks a panel, and a changed word changes what the
board claims. The first mutation run found 265 survivors because tests checked fragments.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from dashboard import board

NOW = int(datetime(2026, 11, 14, 23, 30, tzinfo=UTC).timestamp())
AS_OF = datetime.fromtimestamp(NOW, tz=UTC).isoformat()
STATUS = {"as_of": AS_OF}
LIMIT = board.METAR_STALE_AFTER_S
FRESHNESS = {"most_recent_only": True, "time_resolution_s": 10}
ENTRY = {
    "icao24": "abc123",
    "callsign": "SAS1",
    "latitude": 57.7,
    "longitude": 12.3,
    "on_ground": False,
    "true_track_deg": 90.0,
    "last_contact": NOW - 30,
}
DRAWN = {
    "icao24": "abc123",
    "callsign": "SAS1",
    "latitude": 57.7,
    "longitude": 12.3,
    "on_ground": False,
    "track": 90.0,
    "age_s": 30,
    "fade": 0.75,
}
COLUMNS = list(ENTRY)
ROW = list(ENTRY.values())


def seen(age_s: int | None) -> dict[str, Any]:
    return {"state": "reported", "age_s": age_s, "answered_at": float(NOW)}


def line(source: str, label: str, state: str, text: str) -> dict[str, str]:
    return {"source": source, "label": label, "state": state, "text": text}


def metar(age_s: int | None, *, current: bool = True, category: str = "VFR") -> dict[str, Any]:
    return {
        "ok": True,
        "as_of": AS_OF,
        "metar": [{"flight_category": category, "observation_time": "t", "raw_text": "RAW"}],
        "latest_metar": {"age_s": age_s, "current": current},
        "unknowns": ["gap"],
    }


# --- ages ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (7199, "119 min"),
        (7200, "2 h 0 min"),
        (10_800, "3 h 0 min"),
        (-10_860, "3 h 1 min"),
    ],
)
def test_age_text_changes_unit_at_two_hours(seconds: int, text: str) -> None:
    assert board.age_text(seconds) == text


def test_fade_keeps_two_decimals() -> None:
    assert board.fade(1) == 0.99


# --- weather ---------------------------------------------------------------------------
def test_a_reported_airport_in_full() -> None:
    assert board.weather_view(metar(600)) == {
        "state": "reported",
        "category": "VFR",
        "age_s": 600,
        "answered_at": float(NOW),
        "summary": "VFR, METAR 10 min old",
        "raw": "RAW",
        "unknowns": ["gap"],
    }


def test_a_failed_weather_call_in_full() -> None:
    payload = {"ok": False, "as_of": AS_OF, "error": {"code": "down", "message": "It is down."}}

    assert board.weather_view(payload) == {
        "state": "unknown",
        "category": None,
        "age_s": None,
        "answered_at": float(NOW),
        "summary": "Unknown: weather unavailable (down)",
        "raw": None,
        "unknowns": ["It is down."],
    }


@pytest.mark.parametrize("error", [None, "not a mapping", ["nor", "this"]])
def test_a_failure_without_a_usable_error_still_says_unknown(error: Any) -> None:
    assert board.weather_view({"ok": False, "error": error}) == {
        "state": "unknown",
        "category": None,
        "age_s": None,
        "answered_at": None,
        "summary": "Unknown: weather unavailable (unknown)",
        "raw": None,
        "unknowns": ["Weather could not be read."],
    }


def test_an_answer_with_nothing_in_it_claims_nothing() -> None:
    assert board.weather_view({"ok": True}) == {
        "state": "unknown",
        "category": None,
        "age_s": None,
        "answered_at": None,
        "summary": "Unknown: no METAR returned",
        "raw": None,
        "unknowns": [],
    }


def test_a_report_from_the_future_says_so_exactly() -> None:
    view = board.weather_view(metar(-9000, current=False))

    assert view["summary"] == "Unknown: METAR 2 h 30 min in the future"


def test_a_current_report_without_a_category_is_not_reported() -> None:
    view = board.weather_view(metar(60, category="GREEN"))

    assert (view["state"], view["category"]) == ("unknown", None)
    assert view["age_s"] == 60
    assert view["raw"] == "RAW"


def test_a_report_of_age_zero_that_is_not_current_is_old_not_future() -> None:
    assert board.weather_view(metar(0, current=False))["summary"] == "Unknown: METAR 0 min old"


def test_an_undated_report_is_never_the_newest() -> None:
    payload = metar(60)
    payload["metar"] = [
        {"flight_category": "LIFR", "raw_text": "UNDATED"},
        {"flight_category": "VFR", "observation_time": "2026-11-14T23:20:00Z", "raw_text": "RAW"},
    ]

    assert board.weather_view(payload)["raw"] == "RAW"


def test_a_weather_marker_in_full() -> None:
    assert board.marker({"state": "reported", "category": "IFR"}) == {
        "kind": "weather",
        "category": "IFR",
        "label": "Weather IFR",
        "scope": "weather",
    }


# --- traffic around an airport ---------------------------------------------------------
def traffic(**changes: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "as_of": AS_OF,
        "counts": {"returned": 1},
        "snapshot_current": True,
        "credentials": "anonymous",
        "freshness": FRESHNESS,
        "aircraft": [ENTRY],
        "unknowns": ["gap"],
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "state", "count", "summary", "aircraft"),
    [
        ({}, "heard", 1, "1 heard", [DRAWN]),
        (
            {"snapshot_current": False},
            "stale",
            1,
            "1 heard in a snapshot that is not current",
            [DRAWN],
        ),
        (
            {"counts": {"returned": 0}, "aircraft": []},
            "none_heard",
            0,
            board.NONE_HEARD,
            [],
        ),
        ({"counts": None, "aircraft": None}, "none_heard", 0, board.NONE_HEARD, []),
    ],
)
def test_a_traffic_answer_in_full(
    changes: dict[str, Any], state: str, count: int, summary: str, aircraft: list[Any]
) -> None:
    assert board.traffic_view(traffic(**changes)) == {
        "state": state,
        "count": count,
        "summary": summary,
        "hint": None,
        "credentials": "anonymous",
        "freshness": FRESHNESS,
        "aircraft": aircraft,
        "unknowns": ["gap"],
    }


def test_counts_without_a_returned_figure_mean_none_heard() -> None:
    view = board.traffic_view(traffic(counts={"other": 3}, aircraft=[]))

    assert (view["count"], view["state"]) == (0, "none_heard")


def test_a_failed_traffic_call_in_full() -> None:
    payload = {
        "ok": False,
        "credentials": "none",
        "error": {"code": "denied", "hint": "Set them.", "message": "Refused."},
    }

    assert board.traffic_view(payload) == {
        "state": "unknown",
        "count": None,
        "summary": "Unknown: traffic unavailable (denied)",
        "hint": "Set them.",
        "credentials": "none",
        "freshness": None,
        "aircraft": [],
        "unknowns": ["Refused."],
    }


def test_a_traffic_failure_with_no_detail_still_says_unknown() -> None:
    view = board.traffic_view({"ok": False})

    assert view["summary"] == "Unknown: traffic unavailable (unknown)"
    assert view["unknowns"] == ["Traffic could not be read."]


# --- the wide traffic layer ------------------------------------------------------------
def airspace(**changes: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "as_of": AS_OF,
        "columns": COLUMNS,
        "rows": [ROW],
        "snapshot_current": True,
        "snapshot_time_epoch": NOW,
        "region": "europe",
        "credentials": "anonymous",
        "freshness": FRESHNESS,
        "truncated": True,
        "unknowns": ["gap"],
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "state", "summary", "aircraft"),
    [
        ({}, "heard", "1 heard", [DRAWN]),
        (
            {"snapshot_current": False},
            "stale",
            "1 heard in a snapshot that is not current",
            [DRAWN],
        ),
        ({"rows": []}, "none_heard", board.NONE_HEARD, []),
        ({"rows": None}, "none_heard", board.NONE_HEARD, []),
    ],
)
def test_an_airspace_answer_in_full(
    changes: dict[str, Any], state: str, summary: str, aircraft: list[Any]
) -> None:
    assert board.airspace_view(airspace(**changes)) == {
        "state": state,
        "summary": summary,
        "hint": None,
        "region": "europe",
        "credentials": "anonymous",
        "freshness": FRESHNESS,
        "truncated": True,
        "snapshot_epoch": NOW,
        "aircraft": aircraft,
        "unknowns": ["gap"],
    }


def test_a_failed_airspace_call_in_full() -> None:
    payload = {"ok": False, "error": {"code": "down", "hint": "Later.", "message": "Refused."}}

    assert board.airspace_view(payload) == {
        "state": "unknown",
        "summary": "Unknown: traffic unavailable (down)",
        "hint": "Later.",
        "snapshot_epoch": 0,
        "aircraft": [],
        "unknowns": ["Refused."],
    }


def test_an_airspace_failure_with_no_detail_still_says_unknown() -> None:
    view = board.airspace_view({"ok": False})

    assert view["summary"] == "Unknown: traffic unavailable (unknown)"
    assert view["unknowns"] == ["Traffic could not be read."]


def test_a_row_that_does_not_match_the_columns_is_refused() -> None:
    """Pairing a short row with the columns would put values under the wrong names."""
    with pytest.raises(ValueError, match="zip"):
        board.airspace_view(airspace(rows=[ROW[:-1]]))


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [(1, 1), (board.MAX_SNAPSHOT_EPOCH, board.MAX_SNAPSHOT_EPOCH)],
)
def test_the_edges_of_a_usable_snapshot_time(epoch: int, expected: int) -> None:
    assert board.airspace_view(airspace(snapshot_time_epoch=epoch))["snapshot_epoch"] == expected


# --- triage ----------------------------------------------------------------------------
def test_a_triage_in_full() -> None:
    payload = {
        "ok": True,
        "assessment": {
            "severity": "elevated",
            "reasons": ["gusts"],
            "unknowns": ["radar"],
            "legs_without_evidence": ["traffic"],
        },
        "evidence": {"movements_summary": {"arrivals": 3, "departures": None}},
    }

    assert board.triage_view(payload) == {
        "severity": "elevated",
        "label": "Elevated",
        "reasons": ["gusts"],
        "unknowns": ["radar"],
        "legs_without_evidence": ["traffic"],
        "movements": {"arrivals": "3", "departures": board.MOVEMENTS_NOT_AVAILABLE},
    }


@pytest.mark.parametrize("payload", [{"ok": True}, {"ok": True, "assessment": None}])
def test_a_triage_with_no_assessment_is_unknown(payload: dict[str, Any]) -> None:
    assert board.triage_view(payload) == {
        "severity": "unknown",
        "label": "Unknown",
        "reasons": [],
        "unknowns": [],
        "legs_without_evidence": [],
        "movements": {"arrivals": "not available", "departures": "not available"},
    }


def test_a_failed_triage_in_full() -> None:
    assert board.triage_view({"ok": False, "error": {"message": "Refused."}}) == {
        "severity": "unknown",
        "label": "Unknown",
        "reasons": [],
        "unknowns": ["Refused."],
        "legs_without_evidence": [],
        "movements": {"arrivals": "not available", "departures": "not available"},
    }


def test_a_triage_reports_departures_as_counted() -> None:
    payload = {"ok": True, "evidence": {"movements_summary": {"arrivals": 0, "departures": 5}}}

    assert board.triage_view(payload)["movements"] == {
        "arrivals": board.MOVEMENTS_NOT_AVAILABLE,
        "departures": "5",
    }


def test_a_failed_triage_with_no_detail_says_so() -> None:
    assert board.triage_view({"ok": False})["unknowns"] == ["The triage could not run."]


# --- the banner and the badge ----------------------------------------------------------
def test_the_stale_banner_in_full() -> None:
    view = {"state": "unknown", "age_s": 9000}

    assert board.banner([view, view], None) == {
        "mode": "STALE",
        "label": "STALE: no current METAR among 2 airports",
    }


def test_a_report_dated_in_the_future_in_full() -> None:
    assert board.last_update([seen(-LIMIT - 1)], STATUS, None) == {
        "state": "stale",
        "lines": [line("weather", "Weather", "stale", "stale: dated in the future")],
    }


def test_a_report_exactly_at_the_future_tolerance_still_counts_as_fresh() -> None:
    view = board.last_update([seen(-LIMIT), seen(600)], STATUS, None)

    assert view["lines"][0]["text"] == "Last update under 1 min ago"


def test_a_layer_with_no_usable_time_in_full() -> None:
    assert board.last_update([seen(60)], STATUS, None, 0)["lines"][1] == line(
        "traffic", "Traffic", "unknown", board.SNAPSHOT_TIME_UNKNOWN
    )


def test_a_recording_in_full() -> None:
    view = board.last_update([], STATUS, "2026-11-14T23:30:40+00:00", NOW - 60)

    assert view == {
        "state": "recorded",
        "lines": [
            line("recording", "Recorded", "recorded", "2026-11-14 23:30 UTC"),
            line("traffic", "Traffic", "recorded", "snapshot 2026-11-14 23:29 UTC"),
        ],
    }


@pytest.fixture
def clock_in_tokyo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """CI runs in UTC, where a dropped conversion to UTC changes nothing."""
    monkeypatch.setenv("TZ", "JST-9")
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    monkeypatch.undo()
    if hasattr(time, "tzset"):
        time.tzset()


@pytest.mark.usefixtures("clock_in_tokyo")
def test_times_are_utc_whatever_the_servers_clock_says() -> None:
    assert board.last_update([], STATUS, "2026-11-15T00:30:00+01:00")["lines"][0]["text"] == (
        "2026-11-14 23:30 UTC"
    )

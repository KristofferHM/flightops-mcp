"""Properties of decoding OpenSky's answers.

The rule under test is CLAUDE.md rule 4 in arithmetic form: a row the client
cannot read is counted, never silently dropped, so `received` always equals
what was decoded plus what was not, and a count over the decoded rows is known
to be a lower bound whenever anything was unreadable.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flightops_mcp.errors import ToolFailure
from flightops_mcp.opensky import decode_movements, decode_states

# Any JSON value, the way OpenSky's decoder would see it after `response.json()`.
json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**40), 2**40),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=8),
)
json_values = st.recursive(
    json_scalars,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=6), inner),
    max_leaves=10,
)

icao24 = st.text("0123456789abcdef", min_size=6, max_size=6)
# A documented state row: 17 positional fields, with each one sometimes the wrong type.
state_rows = st.builds(
    lambda address, rest: [address, *rest],
    icao24,
    st.lists(json_scalars, min_size=0, max_size=17),
)
# Things that sit in the `states` list and are not a state vector.
junk_rows = st.one_of(
    st.none(),
    st.just([]),
    st.just([""]),
    st.lists(st.one_of(st.none(), st.integers(), st.floats()), min_size=1, max_size=4),
    st.dictionaries(st.text(max_size=4), json_scalars, max_size=3),
    st.text(max_size=6),
    st.integers(),
)
rows = st.lists(st.one_of(state_rows, junk_rows), max_size=40)


def readable(row: Any) -> bool:
    return isinstance(row, list) and bool(row) and isinstance(row[0], str) and bool(row[0])


@given(states=rows, max_vectors=st.integers(1, 50), snapshot_time=json_values)
def test_every_state_row_is_decoded_or_counted(
    states: list[Any], max_vectors: int, snapshot_time: Any
) -> None:
    snapshot = decode_states({"time": snapshot_time, "states": states}, max_vectors=max_vectors)

    decodable = sum(1 for row in states if readable(row))
    assert snapshot.received == len(states)
    assert snapshot.unreadable == len(states) - decodable
    assert snapshot.unreadable >= 0
    assert len(snapshot.vectors) == min(decodable, max_vectors)
    assert snapshot.truncated == (decodable > max_vectors)
    assert bool(snapshot.notes) == snapshot.truncated
    # The lower bound: what is returned never exceeds what was sent, and falls short
    # of it whenever a row was unreadable or the answer was truncated.
    assert len(snapshot.vectors) <= snapshot.received
    if snapshot.unreadable or snapshot.truncated:
        assert len(snapshot.vectors) < snapshot.received


@given(states=rows, max_vectors=st.integers(1, 50))
def test_decoded_fields_have_the_documented_type_or_are_unknown(
    states: list[Any], max_vectors: int
) -> None:
    snapshot = decode_states({"states": states}, max_vectors=max_vectors)
    for vector in snapshot.vectors:
        assert isinstance(vector.icao24, str)
        assert vector.icao24
        assert isinstance(vector.on_ground, bool)
        for name in (
            "longitude",
            "latitude",
            "baro_altitude_m",
            "velocity_ms",
            "true_track_deg",
            "vertical_rate_ms",
        ):
            value = getattr(vector, name)
            assert value is None or (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
            ), (name, value)
        assert vector.last_contact is None or (
            isinstance(vector.last_contact, int) and not isinstance(vector.last_contact, bool)
        )
        assert vector.callsign is None or vector.callsign == vector.callsign.strip()
        assert vector.origin_country is None or isinstance(vector.origin_country, str)


@given(states=st.lists(state_rows, min_size=2, max_size=40), max_vectors=st.integers(1, 10))
def test_truncation_keeps_the_most_recently_heard(states: list[Any], max_vectors: int) -> None:
    snapshot = decode_states({"states": states}, max_vectors=max_vectors)
    if not snapshot.truncated:
        return
    full = decode_states({"states": states}, max_vectors=len(states))
    kept = [vector.last_contact or 0 for vector in snapshot.vectors]
    every = sorted((vector.last_contact or 0 for vector in full.vectors), reverse=True)
    assert sorted(kept, reverse=True) == every[:max_vectors]


@given(snapshot_time=json_values)
def test_only_an_integer_is_a_snapshot_time(snapshot_time: Any) -> None:
    snapshot = decode_states({"time": snapshot_time, "states": []}, max_vectors=1)
    if isinstance(snapshot_time, int) and not isinstance(snapshot_time, bool):
        assert snapshot.time == snapshot_time
    else:
        assert snapshot.time is None


@given(body=json_values.filter(lambda value: not isinstance(value, dict)))
def test_a_states_answer_that_is_not_an_object_is_unreadable_not_empty(body: Any) -> None:
    with pytest.raises(ToolFailure) as excinfo:
        decode_states(body, max_vectors=10)
    assert excinfo.value.code == "upstream_unavailable"


@given(
    states=json_values.filter(lambda value: value is not None and not isinstance(value, list)),
)
def test_a_states_field_that_is_not_a_list_is_unreadable_not_empty(states: Any) -> None:
    with pytest.raises(ToolFailure) as excinfo:
        decode_states({"states": states}, max_vectors=10)
    assert excinfo.value.code == "upstream_unavailable"


# --- movements ---------------------------------------------------------------
flight_rows = st.fixed_dictionaries(
    {},
    optional={
        "icao24": st.one_of(icao24, json_scalars),
        "callsign": json_scalars,
        "firstSeen": json_scalars,
        "lastSeen": json_scalars,
        "estDepartureAirport": json_scalars,
        "estArrivalAirport": json_scalars,
    },
)
movement_rows = st.lists(st.one_of(flight_rows, junk_rows), max_size=40)


@given(body=movement_rows)
def test_every_movement_row_is_decoded_or_counted(body: list[Any]) -> None:
    page = decode_movements(body)

    flights = sum(1 for row in body if isinstance(row, dict))
    assert page.received == len(body)
    assert len(page.movements) == flights
    assert page.unreadable == len(body) - flights
    for movement in page.movements:
        assert movement.first_seen is None or type(movement.first_seen) is int
        assert movement.last_seen is None or type(movement.last_seen) is int
        assert movement.est_departure_airport is None or isinstance(
            movement.est_departure_airport, str
        )
        assert movement.est_arrival_airport is None or isinstance(movement.est_arrival_airport, str)


def test_a_404_is_an_empty_window_with_nothing_unreadable() -> None:
    page = decode_movements(None)
    assert (page.movements, page.unreadable, page.received) == ([], 0, 0)


@given(body=json_values.filter(lambda value: value is not None and not isinstance(value, list)))
def test_a_movements_answer_that_is_not_an_array_is_unreadable_not_empty(body: Any) -> None:
    with pytest.raises(ToolFailure) as excinfo:
        decode_movements(body)
    assert excinfo.value.code == "upstream_unavailable"

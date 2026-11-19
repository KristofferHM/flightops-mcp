"""Output schemas for the tools a client is most likely to read as data.

Each schema describes the payload the tool returns today, written by hand from the
code that builds it. Rules, each one a test in `tests/test_output_schemas.py`:

* **The payload was not reshaped to fit.** Where a field carries whatever an
  upstream sent (NOAA's station id, its raw report), the schema says "any value"
  rather than inventing a type the code does not enforce.
* **"Unknown" is expressible.** `unknowns` lists, nulls for counts that were never
  made, severity `unknown`, a leg `unavailable` or `not_fetched`: a schema that
  could not say these would force a payload to claim more than it knows.
* **Failures are part of the contract.** Every schema accepts the `ok: false` shape
  that `ToolFailure.to_payload` and the scope check produce.
* **The schema does work.** Objects this server builds are closed
  (`additionalProperties: false`), so a renamed or dropped field fails validation.
  Records passed through from an upstream stay open.

JSON Schema 2020-12, the dialect the protocol assumes when a schema names none.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["OUTPUT_SCHEMAS", "output_schema_for"]

Schema = dict[str, Any]

_STRING: Final[Schema] = {"type": "string"}
_STRINGS: Final[Schema] = {"type": "array", "items": _STRING}
_INT_OR_NULL: Final[Schema] = {"type": ["integer", "null"]}
_NUMBER_OR_NULL: Final[Schema] = {"type": ["number", "null"]}
_STRING_OR_NULL: Final[Schema] = {"type": ["string", "null"]}
# A value copied from an upstream's answer without coercion: any JSON value.
_AS_SENT: Final[Schema] = {"description": "As the upstream sent it; not coerced."}


def _closed(properties: Mapping[str, Schema], *, optional: tuple[str, ...] = ()) -> Schema:
    """An object with exactly these properties; all required except `optional`."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": [name for name in properties if name not in optional],
        "additionalProperties": False,
    }


_FAILURE: Final[Schema] = _closed(
    {
        "ok": {"const": False},
        "error": _closed(
            {
                "code": {**_STRING, "description": "Machine-readable; stable across releases."},
                "message": _STRING,
                "hint": {**_STRING, "description": "What to do next."},
                "retry_after_s": {"type": "number"},
                "details": {"type": "object"},
            },
            optional=("hint", "retry_after_s", "details"),
        ),
    }
)

# Added by the cache to a payload it served instead of the tool (`cache.py`).
_CACHE_BLOCK: Final[Schema] = _closed(
    {
        "hit": {"const": True},
        "ttl_ms": {"type": "integer"},
        "scope": {"enum": ["public", "private"]},
    }
)

_UNKNOWNS: Final[Schema] = {
    **_STRINGS,
    "description": "What this answer cannot show. Non-empty means: do not conclude normality.",
}

_REPORT: Final[Schema] = {
    "description": "One METAR or TAF as decoded from NOAA's JSON.",
    "type": "object",
    "properties": {
        "station_id": _AS_SENT,
        "observation_time": _AS_SENT,
        "raw_text": _AS_SENT,
        "flight_category": _AS_SENT,
        "wx_string": _AS_SENT,
        "temperature_c": _NUMBER_OR_NULL,
        "dewpoint_c": _NUMBER_OR_NULL,
        "wind_dir_deg": _NUMBER_OR_NULL,
        "wind_variable": {"type": "boolean"},
        "wind_speed_kt": _NUMBER_OR_NULL,
        "wind_gust_kt": _NUMBER_OR_NULL,
        "visibility_statute_mi": _NUMBER_OR_NULL,
        "visibility_at_least": {"type": "boolean"},
        "altimeter_hpa": _NUMBER_OR_NULL,
        "clouds": {
            "description": "null: NOAA sent no cloud group. []: it sent an empty one.",
            "type": ["array", "null"],
            "items": _closed({"cover": _STRING, "base_ft": _NUMBER_OR_NULL}),
        },
        "vertical_visibility_ft": _NUMBER_OR_NULL,
        "product": {"enum": ["metar", "taf"]},
    },
    "required": ["station_id", "observation_time", "raw_text", "product"],
}


def _tool(success: Schema) -> Schema:
    """The published schema: the tool's success shape, or the failure shape."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "oneOf": [success, _FAILURE],
    }


_RESOLVE_AIRPORT: Final[Schema] = _closed(
    {
        "ok": {"const": True},
        "query": _STRING,
        "matches": {
            "type": "array",
            "minItems": 1,
            "items": _closed(
                {
                    "icao": _STRING,
                    "iata": _STRING_OR_NULL,
                    "name": _STRING,
                    "type": _STRING_OR_NULL,
                    "municipality": _STRING_OR_NULL,
                    "country": _STRING_OR_NULL,
                    "latitude": _NUMBER_OR_NULL,
                    "longitude": _NUMBER_OR_NULL,
                    "elevation_ft": _INT_OR_NULL,
                }
            ),
        },
        "source": _closed({"name": _STRING, "licence": _STRING, "kind": _STRING}),
        "cache": _CACHE_BLOCK,
    },
    optional=("cache",),
)

_AIRPORT_WEATHER: Final[Schema] = _closed(
    {
        "ok": {"const": True},
        "upstream_time_epoch": {
            **_INT_OR_NULL,
            "description": "NOAA's Date header. null: it sent none, and ages are unknown.",
        },
        "airport": _closed({"icao": _STRING, "name": _STRING}),
        "metar": {"type": "array", "items": _REPORT},
        "taf": {"type": "array", "items": _REPORT},
        "latest_metar": _closed(
            {
                "age_s": {**_INT_OR_NULL, "description": "null: the age cannot be judged."},
                "current": {"type": "boolean"},
            }
        ),
        "impact_flags": _STRINGS,
        "unknowns": _UNKNOWNS,
        "source": _closed({"name": _STRING, "licence": _STRING}),
        "cache": _CACHE_BLOCK,
    },
    optional=("cache",),
)

_LEG: Final[Schema] = _closed(
    {
        "status": {"enum": ["observed", "empty", "not_current", "unavailable", "not_fetched"]},
        "source": _STRING,
        "upstream_time_epoch": _INT_OR_NULL,
        "data_time_epoch": _INT_OR_NULL,
        "age_s": _INT_OR_NULL,
        "error": _STRING_OR_NULL,
    }
)


def _counted(**extra: Schema) -> Schema:
    """A count that was made: readable rows, rows received, and the difference."""
    return _closed(
        {
            "count": {"type": "integer", "minimum": 0},
            "received": {"type": "integer", "minimum": 0},
            "unreadable": {"type": "integer", "minimum": 0},
            **extra,
        }
    )


def _or_null(schema: Schema) -> Schema:
    """`schema`, or null for a value that was never obtained. Zero is a count; null is not."""
    return {"anyOf": [schema, {"type": "null"}]}


_TRIAGE_WEATHER: Final[Schema] = _closed(
    {
        "station": _AS_SENT,
        "observed_at": _AS_SENT,
        "age_s": _INT_OR_NULL,
        "current": {"type": "boolean"},
        "category": _AS_SENT,
        "ceiling": _closed({"ft": _INT_OR_NULL, "basis": _STRING}),
        "visibility": _closed({"statute_mi": _NUMBER_OR_NULL, "at_least": {"type": "boolean"}}),
        "wind": _closed(
            {
                "direction_deg": _NUMBER_OR_NULL,
                "variable": {"type": "boolean"},
                "speed_kt": _NUMBER_OR_NULL,
                "gust_kt": _NUMBER_OR_NULL,
            }
        ),
        "significant_weather": _AS_SENT,
        "raw": _AS_SENT,
    }
)

_DISRUPTION_TRIAGE: Final[Schema] = _closed(
    {
        "ok": {"const": True},
        "upstream_time_epoch": _INT_OR_NULL,
        "credentials": {"enum": ["client_credentials", "anonymous", "none"]},
        "airport": _closed({"icao": _STRING, "name": _STRING, "municipality": _STRING_OR_NULL}),
        "assessment": _closed(
            {
                "severity": {
                    "enum": ["low", "elevated", "high", "unknown"],
                    "description": "unknown outranks elevated: a leg that cannot be seen "
                    "is not evidence of calm.",
                },
                "reasons": _STRINGS,
                "reason_details": {
                    "type": "array",
                    "items": _closed(
                        {
                            "reason": _STRING,
                            "leg": {"enum": ["weather", "traffic", "movements"]},
                            "observations": {"type": "integer", "minimum": 1},
                        }
                    ),
                },
                "why_not_higher": _STRINGS,
                "why_not_lower": _STRINGS,
                "unknowns": _UNKNOWNS,
                "legs_without_evidence": {
                    "type": "array",
                    "items": {"enum": ["weather", "traffic", "movements"]},
                },
                "advisory_only": {"const": True},
            }
        ),
        "counts": _closed(
            {
                "arrivals": _or_null(_counted()),
                "departures": _or_null(_counted()),
                "movements_window_minutes": _INT_OR_NULL,
                "aircraft_in_search_box": _or_null(
                    _counted(
                        listed={"type": "integer", "minimum": 0},
                        airborne=_INT_OR_NULL,
                        on_ground=_INT_OR_NULL,
                        truncated={"type": "boolean"},
                    )
                ),
                "search_radius_nm": {"type": "number"},
            }
        ),
        "weather": _or_null(_TRIAGE_WEATHER),
        "legs": _closed({"weather": _LEG, "traffic": _LEG, "movements": _LEG}),
        "evidence": _closed(
            {
                "weather": _or_null({"type": "array", "items": _REPORT}),
                "movements_summary": _or_null(
                    _closed(
                        {
                            "arrivals": {"type": "integer", "minimum": 0},
                            "departures": {"type": "integer", "minimum": 0},
                        }
                    )
                ),
                "traffic_counts": _or_null(
                    _closed(
                        {
                            "returned": {"type": "integer", "minimum": 0},
                            "airborne": {"type": "integer", "minimum": 0},
                            "on_ground": {"type": "integer", "minimum": 0},
                            "received": {"type": "integer", "minimum": 0},
                            "unreadable": {"type": "integer", "minimum": 0},
                        }
                    )
                ),
            }
        ),
        "degraded": {
            **_STRINGS,
            "description": "Legs that could not be read, as leg:reason.",
        },
        "bookkeeping": _closed(
            {
                "credits_spent": {"type": "integer", "minimum": 0},
                "credits_remaining_upstream": _INT_OR_NULL,
            }
        ),
        "source": _closed({"weather": _STRING, "traffic_and_movements": _STRING}),
    }
)

OUTPUT_SCHEMAS: Final[Mapping[str, Schema]] = {
    "resolve_airport": _tool(_RESOLVE_AIRPORT),
    "airport_weather": _tool(_AIRPORT_WEATHER),
    "disruption_triage": _tool(_DISRUPTION_TRIAGE),
}


def output_schema_for(tool: str) -> Schema | None:
    """A copy of `tool`'s published output schema, or None for a tool without one."""
    schema = OUTPUT_SCHEMAS.get(tool)
    return copy.deepcopy(schema) if schema is not None else None

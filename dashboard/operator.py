"""Who is flying, from the callsign alone: airline, general aviation, military, unknown.

One classifier and one vocabulary. Each aircraft gets a class and the rule that gave it
(`class_evidence`), so a filter on the class can be argued with instead of trusted.

The callsign is the only evidence this board has. OpenSky's `states/all` carries no aircraft
type and no emitter category, so every answer is graded `callsign only`, and the page says so.
What that grade cannot claim is stated rather than hidden:

* A business jet flying under an operator's designator (`NJE123`, NetJets) reads as an airline:
  the callsign is airline-shaped, and without a type there is nothing to say otherwise.
* A helicopter, a glider or a business jet under its registration reads as general aviation.
* **No promotion.** A designator neither table holds stays unknown. It is never guessed into a
  class because the operator sounds like one.

The rules run in order: a known air-force designator, a known airline designator followed by
a flight number, a registration in its country's shape, and otherwise unknown. The tables are
`data/airlines.csv` and `data/operator_designators.csv` (`data/README.md`).
"""

from __future__ import annotations

import csv
import functools
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

Payload = dict[str, Any]

AIRLINE: Final = "airline"
GENERAL: Final = "general aviation"
MILITARY: Final = "military"
UNKNOWN: Final = "unknown"
CLASSES: Final = (AIRLINE, GENERAL, MILITARY, UNKNOWN)
# The chips' words. Shorter than the vocabulary, which the inspector and glossary use whole.
CHIP_LABELS: Final[Mapping[str, str]] = {
    AIRLINE: "Airline",
    GENERAL: "GA",
    MILITARY: "Military",
    UNKNOWN: "Unknown",
}
GLOSSARY: Final[Mapping[str, str]] = {
    AIRLINE: "A callsign that is a known airline designator followed by a flight number.",
    GENERAL: "A callsign that is an aircraft registration, such as SE-MOY or N12345.",
    MILITARY: "A callsign that starts with a known air-force designator.",
    UNKNOWN: "Nothing above matched. Not a fifth kind of operator: no evidence either way.",
}

GRADE: Final = "callsign only"
GRADE_TEXT: Final = (
    "Classed by callsign only: no aircraft type is available, so a business jet flying under "
    "an airline-shaped callsign reads as an airline, a helicopter under its registration reads "
    "as general aviation, and a designator the tables do not hold stays unknown."
)

DATA: Final = Path(__file__).parent / "data"
AIRLINE_TABLE: Final = DATA / "airlines.csv"
DESIGNATOR_TABLE: Final = DATA / "operator_designators.csv"

# A designator followed by a flight number. `RYR` alone has no number and is not a flight.
FLIGHT: Final = re.compile(r"([A-Z]{3})\d[A-Z0-9]{0,3}")
# US registrations: N, a digit other than 0, up to four more digits, up to two letters.
N_NUMBER: Final = re.compile(r"N[1-9]\d{0,4}[A-HJ-NP-Z]{0,2}")
# Nationality marks and how many letters follow them. Only all-letter marks: a callsign
# with a digit in it is read as a flight number, never as a registration.
MARKS: Final[Mapping[str, int]] = {
    **dict.fromkeys(("D", "F", "G", "I"), 4),
    **dict.fromkeys(
        (
            "CS", "EC", "EI", "ES", "HA", "HB", "LN", "LY", "LZ", "OE", "OH", "OK", "OM",
            "OO", "OY", "PH", "SE", "SP", "SX", "TC", "TF", "VH", "YL", "YR", "ZK",
        ),
        3,
    ),
}  # fmt: skip
# Canada: C-F and C-G are the powered-aircraft series.
CANADA: Final = re.compile(r"C[FG][A-Z]{3}")


# Here rather than in aircraft.py, because board.py classifies and aircraft.py imports board.
@functools.cache
def airline_table(path: Path = AIRLINE_TABLE) -> dict[str, tuple[str, str]]:
    """ICAO designator -> (name, country). Read once. See dashboard/data/README.md."""
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["designator"]: (row["name"], row["country"]) for row in csv.DictReader(handle)}


@functools.cache
def designator_table(path: Path = DESIGNATOR_TABLE) -> dict[str, str]:
    """Operator designator -> class, for the operators whose class is not airline."""
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["designator"]: row["class"] for row in csv.DictReader(handle)}


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text or None


def registration(callsign: object) -> str | None:
    """The registration a callsign spells, written with its hyphen, or None.

    `SEMOY` is SE-MOY and `N12345` is N12345. `SAS1396` has a digit after three letters,
    so it is a flight, not a registration.
    """
    text = _clean(callsign)
    if text is None:
        return None
    if N_NUMBER.fullmatch(text):
        return text
    if CANADA.fullmatch(text):
        return f"C-{text[1:]}"
    if not text.isalpha():
        return None
    for mark in (text[:2], text[:1]):
        letters = MARKS.get(mark)
        if letters is not None and len(text) == len(mark) + letters:
            return f"{mark}-{text[len(mark) :]}"
    return None


def classify(callsign: object) -> tuple[str, str]:
    """(operator_class, class_evidence), from the callsign alone."""
    call = _clean(callsign)
    flight = FLIGHT.fullmatch(call or "")
    code = flight.group(1) if flight else None
    if code is not None and code in designator_table():
        return designator_table()[code], f"callsign {call}"
    if code is not None and code in airline_table():
        return AIRLINE, f"callsign {call}"
    reg = registration(call)
    if reg is not None:
        return GENERAL, f"callsign {reg}"
    return UNKNOWN, "nothing matched"


def counts(aircraft: Iterable[Mapping[str, Any]]) -> Payload:
    """The chips' numbers. They describe the region, not the view, so a filter never moves them."""
    tally = dict.fromkeys(CLASSES, 0)
    for entry in aircraft:
        tally[entry.get("operator_class") or UNKNOWN] += 1
    return {
        "all": sum(tally.values()),
        "counts": tally,
        "labels": dict(CHIP_LABELS),
        "classification": GRADE,
        "classification_text": GRADE_TEXT,
    }

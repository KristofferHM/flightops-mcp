"""The licence registry: every data source this server can read, and on what terms.

Like the scope table and the annotation table, this one is closed. An adapter names
its source with `licensed("...")` at module level, so a source with no entry here is
an `ImportError` naming it, raised before any request could be served. There is no
default class: a source whose terms nobody wrote down is not a source.

The classes, from docs/COMMERCIAL.md (terms checked 2026-11-20):

* **A**: public domain. Commercial use permitted; attribution given anyway.
* **B**: open with obligations. Commercial use permitted, with attribution and the
  licence's share-alike duties.
* **C**: commercial terms. A for-profit deployment needs a written licence or a paid
  plan. Served by the demo edition only.
* **D**: customer-supplied. The customer's own licence; never bundled, never cached
  beyond the request, never redistributed.

The wording of `licence_name` and `obligations` is the checked table's, not a
paraphrase: it is what a payload, a footer and the `/licences` page repeat.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "CHECKED_ON",
    "CLASS_MEANING",
    "REGISTRY",
    "Licence",
    "LicenceClass",
    "UnlicensedSourceError",
    "licensed",
    "source_record",
]

LicenceClass = Literal["A", "B", "C", "D"]

# The date the terms below were read from the vendors' own pages.
CHECKED_ON: Final = "2026-11-20"

CLASS_MEANING: Final[Mapping[LicenceClass, str]] = {
    "A": "public domain",
    "B": "open with obligations",
    "C": "commercial terms",
    "D": "customer-supplied",
}


class UnlicensedSourceError(ImportError):
    """A data source with no entry in the registry. An import error, never a default."""

    def __init__(self, source: str) -> None:
        super().__init__(
            f"data source {source!r} has no entry in flightops_mcp.licences.REGISTRY. "
            "Add its licence class, terms and attribution before configuring it."
        )
        self.source = source


@dataclass(frozen=True, slots=True)
class Licence:
    """One source's terms, as this server must honour them."""

    name: str
    licence_class: LicenceClass
    licence_name: str
    attribution: str
    obligations: tuple[str, ...]
    # Whether this server may hold the data past the request that fetched it.
    may_cache: bool
    # Whether the data may be passed on to someone other than the caller.
    may_redistribute: bool
    terms_url: str

    def __post_init__(self) -> None:
        # A D-class source is the customer's under their own licence. Holding it or
        # passing it on is the one thing this server promises never to do.
        if self.licence_class == "D" and (self.may_cache or self.may_redistribute):
            raise ValueError(f"{self.name}: a class D source may not be cached or redistributed")


REGISTRY: Final[Mapping[str, Licence]] = {
    "aviationweather": Licence(
        name="AviationWeather.gov (NOAA)",
        licence_class="A",
        licence_name="US Government work, public domain",
        attribution="Weather: AviationWeather.gov (NOAA), a US Government work in the public "
        "domain. Provided as is, without warranty.",
        obligations=(
            "The API asks that requests be limited in scope and frequency, and points large "
            "queries at its cache files. Operational limits, not a licence fee.",
        ),
        may_cache=True,
        may_redistribute=True,
        terms_url="https://aviationweather.gov/data/api/",
    ),
    "ourairports": Licence(
        name="OurAirports",
        licence_class="A",
        licence_name="Public domain",
        attribution="Airports: OurAirports, public domain.",
        obligations=("No attribution required; we attribute anyway.",),
        may_cache=True,
        may_redistribute=True,
        terms_url="https://ourairports.com/data/",
    ),
    "adsblol": Licence(
        name="adsb.lol",
        licence_class="B",
        licence_name="Open Database License (ODbL) 1.0",
        attribution="Traffic: adsb.lol, made available under the Open Database License "
        "(ODbL) 1.0, https://opendatacommons.org/licenses/odbl/1-0/.",
        obligations=(
            "Commercial use is permitted, but attribution is required.",
            "A derived database used publicly must be offered under ODbL.",
        ),
        may_cache=True,
        may_redistribute=True,
        terms_url="https://api.adsb.lol/docs",
    ),
    "opensky": Licence(
        name="OpenSky Network",
        licence_class="C",
        licence_name="OpenSky Network Terms of Use",
        attribution="Traffic: The OpenSky Network, https://opensky-network.org.",
        obligations=(
            '"Any use by a for-profit or commercial entity requires written permission and '
            'a license."',
            '"Operational use of the REST API in any live product, service, or automated '
            "system also requires a written license, regardless of the entity's non-profit "
            'status."',
        ),
        may_cache=False,
        may_redistribute=False,
        terms_url="https://opensky-network.org/about/terms-of-use",
    ),
    "aviationstack": Licence(
        name="AviationStack",
        licence_class="C",
        licence_name="AviationStack plan terms",
        attribution="Schedules: AviationStack.",
        obligations=(
            "Free tier is 100 requests/month, non-commercial.",
            "Commercial use starts at $49.99/month for 10,000 requests.",
        ),
        may_cache=False,
        may_redistribute=False,
        terms_url="https://aviationstack.com/pricing",
    ),
    "aerodatabox": Licence(
        name="AeroDataBox",
        licence_class="C",
        licence_name="AeroDataBox plan terms",
        attribution="Schedules: AeroDataBox.",
        obligations=(
            "Symbolic pricing, no SLA, explicitly not permitted for mission-critical or "
            "safety-critical use.",
            "Caching, retention, attribution, resale and sublicensing are restricted by plan.",
        ),
        may_cache=False,
        may_redistribute=False,
        terms_url="https://aerodatabox.com/",
    ),
    "schiphol": Licence(
        name="Schiphol Public Flight API",
        licence_class="C",
        licence_name="Schiphol developer terms",
        attribution="Schedule: Amsterdam Airport Schiphol, Public Flight API.",
        obligations=(
            "Partner tier by agreement; the public tier is what the current adapter mocks.",
        ),
        may_cache=False,
        may_redistribute=False,
        terms_url="https://developer.schiphol.nl/",
    ),
    # The operator's own passenger system, reached on behalf of the signed-in user. It
    # is the customer's data under the customer's terms: class D, like an AODB.
    "ops_api": Licence(
        name="Internal operations API",
        licence_class="D",
        licence_name="The operator's own system",
        attribution="Passenger counts: the operator's own operations API.",
        obligations=(
            "Customer-supplied: never bundled, cached beyond the request, or redistributed.",
        ),
        may_cache=False,
        may_redistribute=False,
        terms_url="",
    ),
}


def licensed(source: str) -> Licence:
    """The terms of `source`. Call it at module level, so a missing entry fails import."""
    try:
        return REGISTRY[source]
    except KeyError:
        raise UnlicensedSourceError(source) from None


def source_record(licence: Licence, fetched_at: str) -> dict[str, Any]:
    """What a payload says about one source it was built from."""
    return {
        "name": licence.name,
        "licence_class": licence.licence_class,
        "licence_name": licence.licence_name,
        "attribution": licence.attribution,
        "fetched_at": fetched_at,
    }

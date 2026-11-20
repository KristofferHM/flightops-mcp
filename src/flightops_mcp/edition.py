"""Editions: which licence classes a process may serve, decided once, before it serves.

`FLIGHTOPS_EDITION` is `demo` (every class; never for sale), `commercial` (A and B)
or `byo` (A, B and the customer's own D). `check_edition` reads the settings, finds
every source they configure and the variable that configured it, and either
returns the plan or raises `EditionRefusedError` naming each refused variable, its
source, class and licence.

The gate is at startup and not per call on purpose: a server that boots and then
refuses every request is worse than one that refuses to boot and says why. The
tools check the plan again (`provenance.py`), but only as a second line.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .config import Edition, Settings
from .licences import CLASS_MEANING, REGISTRY, Licence, LicenceClass
from .providers import ProviderAdapter, load_providers

__all__ = [
    "ALLOWED_CLASSES",
    "DEMO_PLAN",
    "ConfiguredSource",
    "EditionPlan",
    "EditionRefusedError",
    "check_edition",
]

ALLOWED_CLASSES: Final[Mapping[Edition, frozenset[LicenceClass]]] = {
    "demo": frozenset({"A", "B", "C", "D"}),
    "commercial": frozenset({"A", "B"}),
    "byo": frozenset({"A", "B", "D"}),
}
COMMERCIAL_USE: Final[Mapping[Edition, str]] = {
    "demo": "not-permitted",
    "commercial": "permitted",
    "byo": "permitted",
}
# Settings that put a source in use, by source. Credentials and switches only: a
# base URL left at its default is not a decision to use anything.
SOURCE_SETTINGS: Final[Mapping[str, tuple[str, ...]]] = {
    "opensky": ("opensky_client_id", "opensky_client_secret", "opensky_anonymous"),
    "schiphol": ("schiphol_client_id", "schiphol_client_secret"),
    "aviationstack": ("aviationstack_access_key",),
    "aerodatabox": ("aerodatabox_api_key",),
    "adsblol": ("adsblol_enabled",),
    "ops_api": ("ops_api_base_url",),
}
# Served by every edition with no setting at all.
ALWAYS_ON: Final[Mapping[str, str]] = {
    "aviationweather": "(built in; no credentials)",
    "ourairports": "(bundled reference data)",
}


@dataclass(frozen=True, slots=True)
class ConfiguredSource:
    """A source this process would use, and the variable that put it in use."""

    source: str
    variable: str


class EditionRefusedError(RuntimeError):
    """The settings configure a source the chosen edition may not serve."""

    def __init__(
        self, edition: Edition, refused: list[ConfiguredSource], licences: Mapping[str, Licence]
    ) -> None:
        allowed = " and ".join(sorted(ALLOWED_CLASSES[edition]))
        lines = [f"FLIGHTOPS_EDITION={edition} refuses to start."]
        for item in refused:
            terms = licences[item.source]
            lines.append(
                f"  {item.variable} configures {terms.name}, licence class "
                f"{terms.licence_class} ({CLASS_MEANING[terms.licence_class]}; "
                f"{terms.licence_name})."
            )
        variables = ", ".join(dict.fromkeys(item.variable for item in refused))
        lines.append(
            f"The {edition} edition serves licence classes {allowed} only. Unset {variables}, "
            "or choose the edition whose terms you hold (docs/COMMERCIAL.md)."
        )
        super().__init__("\n".join(lines))
        self.edition = edition
        self.refused = refused


@dataclass(frozen=True, slots=True)
class EditionPlan:
    """What this process may serve, and from where."""

    edition: Edition
    configured: tuple[ConfiguredSource, ...] = ()
    # Bring-your-own adapters, by name. Each is class D.
    providers: Mapping[str, ProviderAdapter] = field(default_factory=dict)

    @property
    def allowed_classes(self) -> frozenset[LicenceClass]:
        return ALLOWED_CLASSES[self.edition]

    @property
    def commercial_use(self) -> str:
        return COMMERCIAL_USE[self.edition]

    def licence(self, source: str) -> Licence:
        adapter = self.providers.get(source)
        return adapter.licence if adapter is not None else REGISTRY[source]

    def allows(self, source: str) -> bool:
        return self.licence(source).licence_class in self.allowed_classes

    def is_configured(self, source: str) -> bool:
        return source in ALWAYS_ON or any(item.source == source for item in self.configured)

    @property
    def traffic_source(self) -> str | None:
        """Who answers live traffic near an airport, or None when nobody may.

        adsb.lol when it is switched on, in any edition; otherwise OpenSky, which
        only the demo may serve.
        """
        if self.is_configured("adsblol"):
            return "adsblol"
        return "opensky" if self.allows("opensky") else None


def _configured(
    settings: Settings, adapters: Mapping[str, tuple[ProviderAdapter, str]]
) -> list[ConfiguredSource]:
    found = [ConfiguredSource(source, why) for source, why in ALWAYS_ON.items()]
    for source, names in SOURCE_SETTINGS.items():
        for name in names:
            if getattr(settings, name):
                found.append(ConfiguredSource(source, f"FLIGHTOPS_{name.upper()}"))
    found.extend(ConfiguredSource(name, variable) for name, (_, variable) in adapters.items())
    return found


def check_edition(settings: Settings) -> EditionPlan:
    """The plan for `settings`, or `EditionRefusedError` naming every refused variable."""
    adapters = load_providers(settings)
    licences: dict[str, Licence] = {
        **REGISTRY,
        **{name: adapter.licence for name, (adapter, _) in adapters.items()},
    }
    configured = _configured(settings, adapters)
    allowed = ALLOWED_CLASSES[settings.edition]
    refused = [item for item in configured if licences[item.source].licence_class not in allowed]
    if refused:
        raise EditionRefusedError(settings.edition, refused, licences)
    return EditionPlan(
        edition=settings.edition,
        configured=tuple(configured),
        providers={name: adapter for name, (adapter, _) in adapters.items()},
    )


# The plan a server built without settings assumes: the demo, nothing optional on.
DEMO_PLAN: Final = EditionPlan(edition="demo")

"""The bring-your-own boundary: what an adapter must declare, and how it fails."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from flightops_mcp.licences import REGISTRY
from flightops_mcp.providers import (
    FlightsRequest,
    ProviderError,
    ProviderService,
    Quota,
    clear_registered,
    load_providers,
    register_provider,
)
from flightops_mcp.providers_example import EXAMPLE_PROVIDER, LICENCE
from flightops_mcp.telemetry import get_logger, register_secret_values

from .conftest import LogCapture
from .test_commercial import settings_for

REQUEST = FlightsRequest("ESGG", "GOT", ("departure",), 1_763_640_000, 1_763_647_200)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    yield
    clear_registered()


def test_a_provider_must_be_class_d() -> None:
    with pytest.raises(ProviderError, match="declares licence class C"):
        dataclasses.replace(EXAMPLE_PROVIDER, licence=REGISTRY["opensky"])


@pytest.mark.parametrize("name", ["X", "has space", "a" * 40, "Upper"])
def test_a_provider_name_is_a_short_lowercase_word(name: str) -> None:
    with pytest.raises(ProviderError, match="must be 2-32"):
        dataclasses.replace(EXAMPLE_PROVIDER, name=name)


def test_a_quota_allows_at_least_one_request() -> None:
    with pytest.raises(ProviderError):
        Quota(requests_per_hour=0)


def test_registration_refuses_duplicates_and_impostors(tmp_path: Any) -> None:
    register_provider(EXAMPLE_PROVIDER)
    register_provider(EXAMPLE_PROVIDER)  # the same object again is not a duplicate
    with pytest.raises(ProviderError, match="already registered"):
        register_provider(dataclasses.replace(EXAMPLE_PROVIDER))
    with pytest.raises(ProviderError, match="not a ProviderAdapter"):
        register_provider("example")  # type: ignore[arg-type]
    clear_registered()
    register_provider(dataclasses.replace(EXAMPLE_PROVIDER, name="opensky"))
    with pytest.raises(ProviderError, match="name of a registered source"):
        load_providers(settings_for(tmp_path))


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("no_colon_here", "not a module:attribute"),
        ("flightops_mcp_nothing_here:X", "cannot import"),
        ("flightops_mcp.providers_example:LICENCE", "is not a ProviderAdapter"),
    ],
)
def test_a_bad_provider_path_is_refused_by_name(tmp_path: Any, path: str, message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        load_providers(settings_for(tmp_path, byo_providers=path))


def test_two_paths_to_one_name_are_refused(tmp_path: Any) -> None:
    register_provider(dataclasses.replace(EXAMPLE_PROVIDER))
    with pytest.raises(ProviderError, match="two providers"):
        load_providers(settings_for(tmp_path, byo_example_provider=True))


async def test_missing_credentials_are_named_and_never_valued(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLIGHTOPS_EXAMPLE_PROVIDER_KEY", raising=False)
    service = ProviderService(settings_for(tmp_path), {"example": EXAMPLE_PROVIDER})

    with pytest.raises(Exception) as failed:
        await service.flights("example", REQUEST)
    await service.aclose()

    assert failed.value.code == "credentials_missing"  # type: ignore[attr-defined]
    assert "FLIGHTOPS_EXAMPLE_PROVIDER_KEY" in failed.value.message  # type: ignore[attr-defined]


async def test_the_declared_quota_is_a_ceiling(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLIGHTOPS_EXAMPLE_PROVIDER_KEY", "value-1")
    tight = dataclasses.replace(EXAMPLE_PROVIDER, quota=Quota(requests_per_hour=1))
    service = ProviderService(settings_for(tmp_path), {"example": tight})

    flights = await service.flights("example", REQUEST)
    with pytest.raises(Exception) as refused:
        await service.flights("example", REQUEST)
    await service.aclose()

    assert [f.status for f in flights] == ["scheduled", "delayed", "cancelled"]
    assert refused.value.code == "local_budget_exhausted"  # type: ignore[attr-defined]
    assert service.requests_this_hour("example") == 1


def test_the_example_normaliser_skips_what_does_not_fit() -> None:
    raw = {"rows": [{"ident": "TEST1", "kind": "ARR", "sched": 0}, {"sched": "later"}, "x"]}
    flights = EXAMPLE_PROVIDER.normalise(raw, REQUEST)
    assert [(f.flight, f.direction) for f in flights] == [("TEST1", "arrival")]
    assert EXAMPLE_PROVIDER.normalise(None, REQUEST) == []
    assert LICENCE.licence_class == "D"


def test_a_registered_value_is_scrubbed_from_every_log_line(logs: LogCapture) -> None:
    register_secret_values(["CANARY-VALUE-abc", "ab", None])

    get_logger("test").info("calling", url="https://x.test/?key=CANARY-VALUE-abc")

    assert "CANARY-VALUE-abc" not in logs.text
    assert "[REDACTED]" in logs.text

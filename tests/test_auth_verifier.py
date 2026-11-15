"""RS256 verification against a JWKS endpoint.

Every rejection path is asserted twice: the verifier returns None (so the SDK
answers 401), and the reason reaches the log without the token in it.
"""

from __future__ import annotations

import asyncio

import pytest

from flightops_mcp.auth import JwksCache, JwtVerifier

from .authkit import (
    ISSUER,
    JWKS_URL,
    JwksEndpoint,
    forge,
    hs256_with_public_key,
    signing_key,
    tamper,
)
from .conftest import FakeClock, LogCapture

KEY = signing_key("key-1")
ROTATED = signing_key("key-2")


def build(
    endpoint: JwksEndpoint, clock: FakeClock, *, ttl_s: float = 3600.0
) -> tuple[JwtVerifier, JwksCache]:
    jwks = JwksCache(
        JWKS_URL,
        transport=endpoint.transport,
        clock=clock,
        ttl_s=ttl_s,
        min_refresh_interval_s=30.0,
    )
    return JwtVerifier(issuer=ISSUER, jwks=jwks, clock=clock, clock_skew_s=30.0), jwks


@pytest.fixture
def endpoint() -> JwksEndpoint:
    return JwksEndpoint(KEY)


# --- acceptance --------------------------------------------------------------
async def test_a_valid_token_is_accepted_with_its_identity(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    verifier, _ = build(endpoint, clock)
    token = KEY.mint(now=clock(), subject="user-42", scp="a.read b.read", azp="agent-app")

    access = await verifier.verify_token(token)

    assert access is not None
    assert access.subject == "user-42"
    assert access.client_id == "agent-app"
    assert access.scopes == ["a.read", "b.read"]
    assert access.expires_at == int(clock()) + 300
    assert access.claims is not None and access.claims["iss"] == ISSUER


async def test_scopes_are_read_from_delegated_and_application_claims(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    """Entra: `scp` for a user token, `roles` for a service principal."""
    verifier, _ = build(endpoint, clock)

    user = await verifier.verify_token(KEY.mint(now=clock(), scp="a.read"))
    service = await verifier.verify_token(KEY.mint(now=clock(), roles=["b.read", "c.read"]))
    rfc9068 = await verifier.verify_token(KEY.mint(now=clock(), scope="d.read"))
    none = await verifier.verify_token(KEY.mint(now=clock()))

    assert user is not None and user.scopes == ["a.read"]
    assert service is not None and service.scopes == ["b.read", "c.read"]
    assert rfc9068 is not None and rfc9068.scopes == ["d.read"]
    assert none is not None and none.scopes == [], "no scope claim must grant nothing"


# --- validity window ---------------------------------------------------------
async def test_an_expired_token_is_rejected_and_the_reason_is_logged(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    token = KEY.mint(now=clock(), expires_in=300)
    assert await verifier.verify_token(token) is not None

    clock.advance(301)

    assert await verifier.verify_token(token) is None
    rejections = logs.events("token_rejected")
    assert [event["reason"] for event in rejections] == ["expired"]
    assert token not in logs.text


async def test_expiry_has_no_leeway(endpoint: JwksEndpoint, clock: FakeClock) -> None:
    verifier, _ = build(endpoint, clock)
    token = KEY.mint(now=clock(), expires_in=300)

    clock.advance(299)
    assert await verifier.verify_token(token) is not None
    clock.advance(1)
    assert await verifier.verify_token(token) is None, "exp is the first invalid second"


async def test_a_token_that_is_not_yet_valid_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    within_skew = KEY.mint(now=clock(), nbf=int(clock()) + 20)
    beyond_skew = KEY.mint(now=clock(), nbf=int(clock()) + 120)

    assert await verifier.verify_token(within_skew) is not None, "30s of clock skew is tolerated"
    assert await verifier.verify_token(beyond_skew) is None
    assert logs.events("token_rejected")[-1]["reason"] == "not_yet_valid"


# --- signature and algorithm -------------------------------------------------
async def test_a_tampered_payload_fails_the_signature_check(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    token = tamper(KEY.mint(now=clock(), scp="a.read"), scp="a.read admin")

    assert await verifier.verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "bad_signature"


async def test_a_token_signed_by_another_key_under_our_key_id_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    token = ROTATED.mint(now=clock(), kid=KEY.kid)

    assert await verifier.verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "bad_signature"


async def test_hs256_keyed_with_the_public_key_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    """Algorithm confusion: the public key is public, so it must never be an HMAC secret."""
    verifier, _ = build(endpoint, clock)
    now = int(clock())
    token = hs256_with_public_key(
        KEY, {"iss": ISSUER, "sub": "attacker", "iat": now, "exp": now + 300, "scp": "admin"}
    )

    assert await verifier.verify_token(token) is None
    rejection = logs.events("token_rejected")[-1]
    assert rejection["reason"] == "algorithm_not_allowed"
    assert rejection["alg"] == "HS256"
    assert endpoint.calls == 0, "the algorithm is refused before any key is fetched"


async def test_an_unsigned_token_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    now = int(clock())
    token = forge(
        {"alg": "none", "typ": "JWT", "kid": KEY.kid},
        {"iss": ISSUER, "sub": "attacker", "iat": now, "exp": now + 300},
    )

    assert await verifier.verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "algorithm_not_allowed"


# --- claims ------------------------------------------------------------------
async def test_a_token_from_another_issuer_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    token = KEY.mint(now=clock(), issuer="http://evil.test")

    assert await verifier.verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "wrong_issuer"


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "sub"])
async def test_a_token_missing_a_required_claim_is_rejected(
    claim: str, endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    token = KEY.mint(now=clock(), drop=(claim,))

    assert await verifier.verify_token(token) is None
    rejection = logs.events("token_rejected")[-1]
    assert rejection["reason"] == "missing_claim"
    assert rejection["claim"] == claim


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "Bearer", "x" * 4096])
async def test_garbage_is_rejected_without_raising(
    garbage: str, endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    verifier, _ = build(endpoint, clock)
    assert await verifier.verify_token(garbage) is None


async def test_a_token_without_a_key_id_is_rejected(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    now = int(clock())
    token = forge({"alg": "RS256", "typ": "JWT"}, {"iss": ISSUER, "sub": "x", "exp": now + 300})

    assert await verifier.verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == "missing_key_id"


# --- JWKS cache --------------------------------------------------------------
async def test_fifty_concurrent_verifications_fetch_the_key_set_once(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    verifier, jwks = build(endpoint, clock)
    token = KEY.mint(now=clock())

    results = await asyncio.gather(*(verifier.verify_token(token) for _ in range(50)))

    assert all(result is not None for result in results)
    assert endpoint.calls == 1
    assert jwks.fetch_count == 1


async def test_a_rotated_signing_key_is_picked_up_without_a_restart(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    verifier, _ = build(endpoint, clock)
    assert await verifier.verify_token(KEY.mint(now=clock())) is not None

    endpoint.keys = [KEY, ROTATED]
    clock.advance(60)

    assert await verifier.verify_token(ROTATED.mint(now=clock())) is not None
    assert endpoint.calls == 2, "an unknown key id triggers exactly one refetch"


async def test_forged_key_ids_cannot_make_the_server_hammer_the_issuer(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    verifier, _ = build(endpoint, clock)
    assert await verifier.verify_token(KEY.mint(now=clock())) is not None
    clock.advance(60)

    for attempt in range(25):
        forged = ROTATED.mint(now=clock(), kid=f"forged-{attempt}")
        assert await verifier.verify_token(forged) is None

    assert endpoint.calls == 2, "one refetch for the first unknown id, none for the next 24"
    assert logs.events("token_rejected")[-1]["reason"] == "unknown_key_id"

    clock.advance(31)
    assert await verifier.verify_token(ROTATED.mint(now=clock(), kid="forged-late")) is None
    assert endpoint.calls == 3, "the floor is a rate limit, not a permanent block"


async def test_an_unreachable_issuer_rejects_rather_than_raises(
    endpoint: JwksEndpoint, clock: FakeClock, logs: LogCapture
) -> None:
    endpoint.failing = True
    verifier, _ = build(endpoint, clock)

    assert await verifier.verify_token(KEY.mint(now=clock())) is None
    assert logs.events("jwks_fetch_failed"), "the outage itself must be visible in the log"
    assert logs.events("token_rejected")[-1]["reason"] == "unknown_key_id"


async def test_known_keys_survive_an_issuer_outage(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    verifier, _ = build(endpoint, clock, ttl_s=600.0)
    assert await verifier.verify_token(KEY.mint(now=clock())) is not None

    endpoint.failing = True
    clock.advance(601)  # the cache is now stale and cannot be refreshed

    assert await verifier.verify_token(KEY.mint(now=clock())) is not None
    assert endpoint.calls == 2, "a refresh was attempted, and its failure was tolerated"


async def test_non_rsa_and_malformed_jwks_entries_are_ignored(
    endpoint: JwksEndpoint, clock: FakeClock
) -> None:
    endpoint.extra_entries = [
        {"kty": "oct", "kid": "symmetric", "k": "c2VjcmV0"},
        {"kty": "RSA", "kid": "encryption-only", "use": "enc", "n": "AQAB", "e": "AQAB"},
        {"kty": "RSA", "kid": "broken"},
        "not-an-object",
    ]
    verifier, jwks = build(endpoint, clock)

    assert await verifier.verify_token(KEY.mint(now=clock())) is not None
    assert await jwks.key_for("symmetric") is None
    assert await jwks.key_for("encryption-only") is None
    assert await jwks.key_for("broken") is None

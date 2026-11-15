"""The local IdP: client credentials, RFC 8693 token exchange, revoke and expire.

Driven over HTTP through the real FastAPI app. The property that matters most is
at the bottom of the exchange section: a token can be narrowed, never widened.
"""

from __future__ import annotations

import pytest

from flightops_mcp.auth import JwksCache, JwtVerifier
from flightops_mcp.config import Settings
from services.idp.provider import ACCESS_TOKEN_TYPE

from .authkit import ISSUER, JWKS_URL, signing_key
from .conftest import FakeClock
from .harness import remote_settings, serve
from .idpkit import (
    AGENT,
    AGENT_ROLES,
    BYSTANDER,
    MCP_AUDIENCE,
    OPS_AUDIENCE,
    OTHER_AUDIENCE,
    PASSENGERS_READ,
    SERVER,
    USER_SCOPES,
    LocalIdp,
    claims_of,
)
from .test_server_tools import EXPECTED_TOOLS, install


@pytest.fixture
def idp() -> LocalIdp:
    return LocalIdp()


def verifier_for(idp: LocalIdp) -> JwtVerifier:
    return JwtVerifier(
        issuer=ISSUER, audience=MCP_AUDIENCE, jwks=JwksCache(JWKS_URL, transport=idp.transport)
    )


# --- metadata ----------------------------------------------------------------
async def test_discovery_points_at_the_endpoints(idp: LocalIdp) -> None:
    async with idp.http() as client:
        for path in ("openid-configuration", "oauth-authorization-server"):
            document = (await client.get(f"/.well-known/{path}")).json()
            assert document["issuer"] == ISSUER
            assert document["jwks_uri"] == JWKS_URL
            assert document["token_endpoint"] == f"{ISSUER}/token"
            assert (
                "urn:ietf:params:oauth:grant-type:token-exchange"
                in (document["grant_types_supported"])
            )


async def test_the_jwks_publishes_one_public_rsa_key_and_nothing_private(idp: LocalIdp) -> None:
    async with idp.http() as client:
        keys = (await client.get("/.well-known/jwks.json")).json()["keys"]

    assert len(keys) == 1
    key = keys[0]
    assert (key["kty"], key["use"], key["alg"]) == ("RSA", "sig", "RS256")
    assert set(key) == {"kty", "use", "alg", "kid", "n", "e"}, "no private parameters may leak"


async def test_every_idp_instance_signs_with_its_own_key() -> None:
    """The default, as the service runs: a fresh key per process, nothing shared or stored."""
    assert LocalIdp(own_key=True).provider.kid != LocalIdp(own_key=True).provider.kid


# --- client credentials ------------------------------------------------------
async def test_client_credentials_issues_an_application_token(idp: LocalIdp) -> None:
    response = await idp.token(AGENT, grant_type="client_credentials", resource=MCP_AUDIENCE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    claims = claims_of(body["access_token"])
    assert claims["iss"] == ISSUER
    assert claims["aud"] == MCP_AUDIENCE
    assert claims["sub"] == claims["azp"] == AGENT
    assert claims["roles"] == AGENT_ROLES
    assert claims["tid"] == idp.config.tenant_id
    assert "scp" not in claims, "an application token carries roles, not delegated scopes"


async def test_the_entra_default_scope_form_names_the_audience(idp: LocalIdp) -> None:
    response = await idp.token(
        AGENT, grant_type="client_credentials", scope=f"{MCP_AUDIENCE}/.default"
    )

    assert response.status_code == 200
    assert claims_of(response.json()["access_token"])["aud"] == MCP_AUDIENCE


async def test_a_client_may_ask_for_fewer_roles_but_not_for_more(idp: LocalIdp) -> None:
    fewer = await idp.token(
        AGENT,
        grant_type="client_credentials",
        resource=MCP_AUDIENCE,
        scope="flightops.weather.read",
    )
    more = await idp.token(
        AGENT, grant_type="client_credentials", resource=MCP_AUDIENCE, scope=PASSENGERS_READ
    )

    assert claims_of(fewer.json()["access_token"])["roles"] == ["flightops.weather.read"]
    assert more.status_code == 400
    assert more.json()["error"] == "invalid_scope"


async def test_a_client_gets_no_token_for_an_audience_it_has_no_role_on(idp: LocalIdp) -> None:
    response = await idp.token(AGENT, grant_type="client_credentials", resource=OPS_AUDIENCE)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"


async def test_client_secret_post_works_as_well_as_basic(idp: LocalIdp) -> None:
    async with idp.http() as client:
        response = await client.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "resource": MCP_AUDIENCE,
                "client_id": AGENT,
                "client_secret": idp.secrets[AGENT],
            },
        )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("client_id", "secret"),
    [(AGENT, "wrong"), ("nobody", "anything"), (AGENT, ""), ("", "")],
)
async def test_bad_client_authentication_is_a_401(
    client_id: str, secret: str, idp: LocalIdp
) -> None:
    async with idp.http() as client:
        response = await client.post(
            "/token",
            data={"grant_type": "client_credentials", "resource": MCP_AUDIENCE},
            auth=(client_id, secret),
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"
    assert "www-authenticate" in response.headers


@pytest.mark.parametrize("grant_type", ["password", "authorization_code", "refresh_token", ""])
async def test_other_grant_types_are_refused(grant_type: str, idp: LocalIdp) -> None:
    response = await idp.token(AGENT, grant_type=grant_type)

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


# --- tokens are what the resource server expects ------------------------------
async def test_the_resource_server_verifier_accepts_idp_tokens(idp: LocalIdp) -> None:
    verifier = verifier_for(idp)

    service = await verifier.verify_token(await idp.service_token())
    user = await verifier.verify_token(await idp.user_token("user-7"))

    assert service is not None and service.scopes == AGENT_ROLES
    assert service.client_id == AGENT
    assert user is not None and user.subject == "user-7"
    assert user.scopes == USER_SCOPES.split()


async def test_a_token_from_another_idp_instance_is_rejected(idp: LocalIdp) -> None:
    """Same issuer name, different key: only the published key counts."""
    impostor = LocalIdp(own_key=True)

    assert await verifier_for(idp).verify_token(await impostor.service_token()) is None


async def test_the_mcp_server_serves_a_service_principal_from_the_local_idp(
    idp: LocalIdp, settings: Settings
) -> None:
    configured = remote_settings(settings)
    install(configured)
    token = await idp.service_token()

    async with serve(configured, idp.transport) as remote, remote.client(token) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS


# --- token exchange (RFC 8693) -----------------------------------------------
async def test_exchange_returns_a_narrower_token_for_the_downstream_api(idp: LocalIdp) -> None:
    subject = await idp.user_token("user-9")

    response = await idp.exchange(subject, scope=PASSENGERS_READ)

    assert response.status_code == 200
    body = response.json()
    assert body["issued_token_type"] == ACCESS_TOKEN_TYPE
    assert body["token_type"] == "Bearer"
    assert body["scope"] == PASSENGERS_READ
    exchanged = claims_of(body["access_token"])
    assert exchanged["aud"] == OPS_AUDIENCE
    assert exchanged["sub"] == "user-9", "the user's identity, not the server's, goes downstream"
    assert exchanged["scp"] == PASSENGERS_READ
    assert exchanged["act"] == {"sub": SERVER}
    assert exchanged["azp"] == SERVER
    assert set(exchanged["scp"].split()) < set(claims_of(subject)["scp"].split())
    assert exchanged["jti"] != claims_of(subject)["jti"]


async def test_exchange_cannot_add_a_scope_the_user_never_had(idp: LocalIdp) -> None:
    """The escalation attempt: the target would accept the scope, the user does not hold it."""
    subject = await idp.user_token(scope="flightops.weather.read")

    response = await idp.exchange(subject, scope=PASSENGERS_READ)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"
    assert "access_token" not in response.json()
    assert idp.provider.issued["token_exchange"] == 0


async def test_exchange_cannot_carry_a_scope_the_target_does_not_accept(idp: LocalIdp) -> None:
    subject = await idp.user_token()

    response = await idp.exchange(subject, scope=f"{PASSENGERS_READ} flightops.weather.read")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


async def test_exchange_cannot_invent_a_scope(idp: LocalIdp) -> None:
    subject = await idp.user_token()

    response = await idp.exchange(subject, scope="flightops.passengers.write")

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


async def test_exchange_without_a_scope_request_grants_only_the_intersection(
    idp: LocalIdp,
) -> None:
    response = await idp.exchange(await idp.user_token())

    assert claims_of(response.json()["access_token"])["scp"] == PASSENGERS_READ


async def test_exchange_with_nothing_in_the_intersection_fails(idp: LocalIdp) -> None:
    response = await idp.exchange(await idp.user_token(scope="flightops.weather.read"))

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


async def test_an_exchanged_token_cannot_outlive_the_token_it_came_from(idp: LocalIdp) -> None:
    short = await idp.user_token(expires_in=60)
    long = await idp.user_token(expires_in=3600)

    from_short = (await idp.exchange(short)).json()
    from_long = (await idp.exchange(long)).json()

    assert from_short["expires_in"] <= 60
    assert claims_of(from_short["access_token"])["exp"] <= claims_of(short)["exp"]
    assert from_long["expires_in"] == idp.config.exchanged_token_ttl_s


async def test_a_client_cannot_exchange_a_token_that_was_not_issued_to_it(idp: LocalIdp) -> None:
    """Confused deputy: the bystander has exchange rights, but this token is the server's."""
    subject = await idp.user_token(audience=MCP_AUDIENCE)

    response = await idp.exchange(subject, client_id=BYSTANDER)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_an_application_token_cannot_be_exchanged(idp: LocalIdp) -> None:
    """On-behalf-of needs a user to be on behalf of."""
    response = await idp.exchange(await idp.service_token())

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_exchange_for_an_audience_outside_the_allow_list_fails(idp: LocalIdp) -> None:
    response = await idp.exchange(await idp.user_token(), audience=OTHER_AUDIENCE)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"


async def test_a_client_without_exchange_rights_cannot_exchange(idp: LocalIdp) -> None:
    response = await idp.exchange(await idp.user_token(), client_id=AGENT)

    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_client"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"subject_token": ""}, "invalid_request"),
        (
            {"subject_token_type": "urn:ietf:params:oauth:token-type:id_token"},
            "invalid_request",
        ),
        (
            {"requested_token_type": "urn:ietf:params:oauth:token-type:refresh_token"},
            "invalid_request",
        ),
        ({"subject_token": "not-a-jwt"}, "invalid_grant"),
    ],
)
async def test_malformed_exchange_requests_are_refused(
    overrides: dict[str, str], error: str, idp: LocalIdp
) -> None:
    response = await idp.exchange(await idp.user_token(), **overrides)

    assert response.status_code == 400
    assert response.json()["error"] == error


async def test_a_subject_token_signed_by_someone_else_is_refused(idp: LocalIdp) -> None:
    forged = signing_key("key-1").mint(scp=USER_SCOPES, jti="forged", kid=idp.provider.kid)

    response = await idp.exchange(forged)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_exchange_requires_an_audience(idp: LocalIdp) -> None:
    response = await idp.exchange(await idp.user_token(), audience=None)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"


# --- revoke and expire -------------------------------------------------------
async def test_a_revoked_token_can_no_longer_be_exchanged(idp: LocalIdp) -> None:
    subject = await idp.user_token()
    assert (await idp.exchange(subject)).status_code == 200

    revoked = await idp.revoke(subject)

    assert revoked.status_code == 200
    response = await idp.exchange(subject)
    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_grant",
        "error_description": "subject token is revoked",
    }


async def test_revocation_is_silent_about_what_it_did(idp: LocalIdp) -> None:
    """RFC 7009: the same 200 for garbage and for another client's token."""
    subject = await idp.user_token(client_id=AGENT)

    garbage = await idp.revoke("not-a-token")
    not_mine = await idp.revoke(subject, client_id=BYSTANDER)

    assert (garbage.status_code, garbage.json()) == (200, {})
    assert (not_mine.status_code, not_mine.json()) == (200, {})
    assert (await idp.exchange(subject)).status_code == 200, "only the owning client can revoke"


async def test_revocation_requires_client_authentication(idp: LocalIdp) -> None:
    async with idp.http() as client:
        response = await client.post("/revoke", data={"token": await idp.user_token()})

    assert response.status_code == 401


async def test_a_token_expired_on_demand_can_no_longer_be_exchanged(idp: LocalIdp) -> None:
    subject = await idp.user_token()

    expired = await idp.expire(subject)

    assert expired.json() == {"expired": True}
    response = await idp.exchange(subject)
    assert response.status_code == 400
    assert response.json()["error_description"] == "subject token is expired"
    assert (await idp.expire("not-a-token")).json() == {"expired": False}


async def test_a_token_minted_already_expired_is_refused_everywhere(idp: LocalIdp) -> None:
    """The on-demand expired token: correctly signed, so only the clock rejects it."""
    token = await idp.user_token(expires_in=-60)

    assert await verifier_for(idp).verify_token(token) is None
    assert (await idp.exchange(token)).json()["error"] == "invalid_grant"


async def test_expiry_follows_the_idp_clock(clock: FakeClock) -> None:
    idp = LocalIdp(clock)
    subject = await idp.user_token(expires_in=120)
    assert (await idp.exchange(subject)).status_code == 200

    clock.advance(121)

    assert (await idp.exchange(subject)).json()["error"] == "invalid_grant"

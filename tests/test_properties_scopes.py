"""Properties of the scope model: parsing a token's grants, the subset rule, the exchange.

Three questions, each over inputs a person would not think to write:

* What does a token grant? `scopes_from_claims` reads `scp`, `scope` and `roles`.
  Duplicates collapse, order is kept, case is significant, and an empty or
  wrongly typed claim grants nothing.
* What is missing for a tool? `Scopes.missing` is exactly the required set minus
  the granted one. Unknown, duplicated and differently cased grants change nothing.
* What can an exchange carry? The IdP issues a token whose scopes are a subset of
  both the subject token's and the target's, or it issues nothing.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from flightops_mcp.auth import Scopes, UnknownToolError
from flightops_mcp.auth.verifier import scopes_from_claims
from services.idp.config import ClientConfig, IdpConfig
from services.idp.provider import ACCESS_TOKEN_TYPE, IdentityProvider, OAuthError

from .conftest import FakeClock

KNOWN = sorted(Scopes.all() | {Scopes.REBOOKING_APPROVE})
TOOLS = sorted(Scopes._BY_TOOL)

# A scope token: one of ours, one of ours in another case, or something invented.
scope_tokens = st.one_of(
    st.sampled_from(KNOWN),
    st.sampled_from(KNOWN).map(str.upper),
    st.sampled_from(KNOWN).map(str.title),
    st.from_regex(r"[a-z]{1,8}(\.[a-z]{1,8}){0,3}", fullmatch=True),
)
separators = st.text(" \t\n", min_size=1, max_size=3)


@st.composite
def delimited(draw: st.DrawFn, tokens: list[str]) -> str:
    """`tokens` joined the way a sloppy issuer might: any whitespace, any amount, padded."""
    gaps = draw(st.lists(separators, min_size=len(tokens) + 1, max_size=len(tokens) + 1))
    return gaps[0] + "".join(token + gap for token, gap in zip(tokens, gaps[1:], strict=True))


# --- what a token grants -----------------------------------------------------
@given(
    data=st.data(), scp=st.lists(scope_tokens, max_size=8), scope=st.lists(scope_tokens, max_size=8)
)
def test_space_delimited_claims_are_split_deduplicated_and_kept_in_order(
    data: st.DataObject, scp: list[str], scope: list[str]
) -> None:
    claims = {"scp": data.draw(delimited(scp)), "scope": data.draw(delimited(scope))}

    granted = scopes_from_claims(claims)

    assert granted == list(dict.fromkeys(scp + scope))
    assert len(granted) == len(set(granted))
    assert all(token and not any(c.isspace() for c in token) for token in granted)


@given(roles=st.lists(scope_tokens, max_size=8))
def test_roles_are_taken_one_per_entry(roles: list[str]) -> None:
    assert scopes_from_claims({"roles": roles}) == list(dict.fromkeys(roles))


@given(
    junk=st.one_of(
        st.none(), st.integers(), st.booleans(), st.lists(st.integers(), max_size=3), st.just({})
    )
)
def test_a_scope_claim_of_the_wrong_type_grants_nothing(junk: Any) -> None:
    assert scopes_from_claims({"scp": junk, "scope": junk}) == []


@given(
    roles=st.lists(
        st.one_of(
            st.integers(),
            st.none(),
            st.booleans(),
            st.just(""),
            st.just("  "),
            st.just("two words"),
            st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
        ),
        max_size=6,
    )
)
def test_a_role_that_is_not_one_scope_name_grants_nothing(roles: list[Any]) -> None:
    """`str(role)` used to turn `[1, None, ""]` into the grants "1", "None" and ""."""
    assert scopes_from_claims({"roles": roles}) == []


@given(claims=st.dictionaries(st.sampled_from(["scp", "scope", "roles", "other"]), st.just("")))
def test_empty_claims_grant_nothing(claims: dict[str, str]) -> None:
    assert scopes_from_claims(claims) == []


# --- what is missing for a tool ---------------------------------------------
@given(tool=st.sampled_from(TOOLS), granted=st.lists(scope_tokens, max_size=12))
def test_missing_is_exactly_required_minus_granted(tool: str, granted: list[str]) -> None:
    required = Scopes.for_tool(tool)
    missing = Scopes.missing(tool, granted)

    assert missing == sorted(required - set(granted))
    assert set(missing) <= required
    assert (missing == []) == required.issubset(granted)


@given(
    tool=st.sampled_from(TOOLS),
    granted=st.lists(st.sampled_from(KNOWN), max_size=8),
    noise=st.lists(scope_tokens, max_size=8),
)
def test_grants_that_are_not_required_change_nothing(
    tool: str, granted: list[str], noise: list[str]
) -> None:
    """Unknown scopes, other tools' scopes, duplicates and order are all irrelevant."""
    required = Scopes.for_tool(tool)
    irrelevant = [token for token in noise if token not in required]
    assert Scopes.missing(tool, granted) == Scopes.missing(
        tool, list(reversed(granted)) + irrelevant + granted
    )


@given(tool=st.sampled_from(TOOLS), case=st.sampled_from([str.upper, str.title, str.swapcase]))
def test_a_scope_in_another_case_is_another_scope(tool: str, case: Any) -> None:
    required = Scopes.for_tool(tool)
    assert Scopes.missing(tool, [case(scope) for scope in required]) == sorted(required)


@given(tool=st.sampled_from(TOOLS))
def test_every_tool_needs_something_and_nothing_needs_the_approval_scope(tool: str) -> None:
    required = Scopes.for_tool(tool)
    assert required
    assert required <= Scopes.all()
    assert Scopes.REBOOKING_APPROVE not in required
    # Holding every scope but one of a tool's is never enough.
    for scope in required:
        assert Scopes.missing(tool, Scopes.all() - {scope}) == [scope]


@given(name=st.text(max_size=30).filter(lambda name: name not in TOOLS))
def test_a_tool_outside_the_table_is_an_error_not_an_empty_requirement(name: str) -> None:
    with pytest.raises(UnknownToolError):
        Scopes.for_tool(name)
    with pytest.raises(UnknownToolError):
        Scopes.missing(name, KNOWN)


# --- what an exchange can carry ----------------------------------------------
SERVER = "flightops-mcp"
MCP = "http://localhost:8000/mcp"
OPS = "api://flightops-ops"
UNIVERSE = [*KNOWN, "flightops.crew.read", "openid"]


@functools.cache
def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def provider(allowed: list[str]) -> tuple[IdentityProvider, ClientConfig]:
    client = ClientConfig(
        client_id=SERVER,
        # A fixture value for an in-process IdP that signs with a throwaway key.
        client_secret=SecretStr("property-test-only"),
        resource=MCP,
        exchange_targets={OPS: allowed},
    )
    config = IdpConfig(issuer="http://idp.test", clients=[client])
    return IdentityProvider(config, clock=FakeClock(), private_key=_key()), client


@given(
    subject=st.lists(st.sampled_from(UNIVERSE), min_size=1, max_size=6, unique=True),
    allowed=st.lists(st.sampled_from(UNIVERSE), max_size=6, unique=True),
    requested=st.one_of(
        st.none(), st.lists(st.sampled_from(UNIVERSE), min_size=1, max_size=6, unique=True)
    ),
)
def test_an_exchanged_token_never_carries_more_than_its_subject(
    subject: list[str], allowed: list[str], requested: list[str] | None
) -> None:
    idp, client = provider(allowed)
    user = idp.mint_user_token(sub="user-1", audience=MCP, scope=" ".join(subject), client_id="a")
    ceiling = [scope for scope in subject if scope in allowed]

    try:
        issued = idp.exchange(
            client,
            subject_token=user.access_token,
            subject_token_type=ACCESS_TOKEN_TYPE,
            audience=OPS,
            scope=None if requested is None else " ".join(requested),
        )
    except OAuthError as exc:
        # Refused, and only for a reason that is about the scopes.
        assert exc.error == "invalid_scope"
        wanted = ceiling if requested is None else requested
        assert not wanted or not set(wanted) <= set(ceiling)
        assert idp.issued["token_exchange"] == 0
        return

    carried = issued.claims["scp"].split()
    assert carried
    assert set(carried) <= set(subject)
    assert set(carried) <= set(allowed)
    assert carried == (ceiling if requested is None else requested)
    assert issued.claims["sub"] == "user-1"
    assert issued.claims["act"] == {"sub": SERVER}
    assert issued.expires_in <= user.expires_in

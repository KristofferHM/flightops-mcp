"""auth/ contracts that mutation testing found unpinned (docs/QUALITY.md, section 3).

Grouped by component. The ones that were genuine gaps rather than detail:

* Neither builder was ever tested with an issuer the token did not match, so
  passing `issuer=None` through (which turns PyJWT's issuer check off) survived.
* The verifier's own clock was the only one asserted, never that PyJWT's is off:
  a token valid by the injected clock but "in the future" by the wall clock must
  still be accepted, or the suite is testing two clocks at once.
* The approval audit log (`approval_rejected`) was asserted for two reasons out
  of seven, so most of the trail an operator relies on could vanish unnoticed.
* The scope challenge's body reader, response headers and pass-through paths were
  exercised only through the full HTTP stack, where most of their edges never occur.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

from flightops_mcp.auth import (
    ApprovalVerifier,
    AuthConfigError,
    JwksCache,
    JwtVerifier,
    ScopeChallengingServer,
    Scopes,
    TokenRejected,
    UnknownToolError,
    build_approval_verifier,
    build_resource_server_auth,
)
from flightops_mcp.auth import approval as approval_module
from flightops_mcp.auth.challenge import INSUFFICIENT_SCOPE_RPC_CODE, ScopeChallengeMiddleware
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.rebooking import approved_digest

from .authkit import ISSUER, JWKS_URL, RESOURCE, JwksEndpoint, forge, signing_key
from .conftest import FakeClock, LogCapture
from .test_approval import APPROVER, AUDIENCE, PROPOSAL, PROPOSER, detail, grant

KEY = signing_key("contract-key")
APPROVAL_KEY = signing_key("approval-key")


def verifier(clock: FakeClock, **kwargs: Any) -> JwtVerifier:
    jwks = JwksCache(JWKS_URL, transport=JwksEndpoint(KEY).transport, clock=clock)
    return JwtVerifier(issuer=ISSUER, audience=RESOURCE, jwks=jwks, clock=clock, **kwargs)


def configured(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"auth_issuer": ISSUER, "auth_jwks_url": JWKS_URL, **update})


# --- the verifier ------------------------------------------------------------
async def test_the_injected_clock_is_the_only_clock(logs: LogCapture) -> None:
    """Issued and valid-from in 2040 by the verifier's clock: PyJWT must not second-guess it."""
    future = FakeClock(2_208_988_800.0)  # 2040-01-01
    token = KEY.mint(now=future(), nbf=int(future()))
    assert await verifier(future).verify_token(token) is not None


async def test_not_before_allows_the_skew_and_not_a_moment_more(clock: FakeClock) -> None:
    check = verifier(clock)  # default skew: 30 s
    at_edge = KEY.mint(now=clock(), nbf=int(clock()) + 30)
    past_edge = KEY.mint(now=clock(), nbf=int(clock()) + 31)
    assert await check.verify_token(at_edge) is not None
    assert await check.verify_token(past_edge) is None


@pytest.mark.parametrize("claim", ["exp", "nbf"])
@pytest.mark.parametrize("value", ["9999999999", True, None, [1]])
async def test_a_time_claim_that_is_not_a_number_is_rejected_and_named(
    clock: FakeClock, logs: LogCapture, claim: str, value: Any
) -> None:
    token = KEY.mint(now=clock(), **{claim: value})
    assert await verifier(clock).verify_token(token) is None
    event = logs.events("token_rejected")[-1]
    assert event["reason"] in {"invalid", "missing_claim"}
    if event["reason"] == "invalid":
        assert event["claim"] == claim


async def test_rejections_carry_what_an_operator_needs(clock: FakeClock, logs: LogCapture) -> None:
    check = verifier(clock)
    await check.verify_token("not a jwt")
    await check.verify_token(KEY.mint(now=clock(), kid="unknown-kid"))
    other = signing_key("contract-other")
    await check.verify_token(other.mint(now=clock(), kid=KEY.kid))
    expired = KEY.mint(now=clock() - 400, expires_in=300)
    clock.advance(0.25)  # expired for 100.25 s: the log rounds to one decimal
    await check.verify_token(expired)
    # Correctly signed, but the payload is a JSON array: PyJWT's generic error path.
    await check.verify_token(
        jwt.api_jws.encode(b"[1, 2]", KEY._private, algorithm="RS256", headers={"kid": KEY.kid})
    )

    events = logs.events("token_rejected")
    by_reason = {event["reason"]: event for event in events}
    assert "malformed" in by_reason
    assert by_reason["unknown_key_id"]["kid"] == "unknown-kid"
    assert by_reason["bad_signature"]["kid"] == KEY.kid
    assert by_reason["expired"]["expired_for_s"] == 100.2
    assert by_reason["invalid"]["error_class"] == "DecodeError"


@pytest.mark.parametrize(
    ("kid", "reason"),
    # PyJWT refuses a header whose `kid` is not a string before our check runs; an empty
    # string reaches it, and is the case that tells "missing" from "unknown".
    [("", "missing_key_id"), (5, "malformed"), (None, "malformed"), (["key"], "malformed")],
)
async def test_a_key_id_that_is_not_a_non_empty_string_is_refused_before_a_lookup(
    clock: FakeClock, logs: LogCapture, kid: Any, reason: str
) -> None:
    token = forge({"alg": "RS256", "typ": "JWT", "kid": kid}, {"sub": "x"})
    assert await verifier(clock).verify_token(token) is None
    assert logs.events("token_rejected")[-1]["reason"] == reason


async def test_the_client_is_azp_then_appid_then_sub(clock: FakeClock) -> None:
    check = verifier(clock)
    both = await check.verify_token(KEY.mint(now=clock(), azp="by-azp", appid="by-appid"))
    appid = await check.verify_token(KEY.mint(now=clock(), appid="by-appid"))
    neither = await check.verify_token(KEY.mint(now=clock(), subject="by-sub"))
    assert both is not None and both.client_id == "by-azp"
    assert appid is not None and appid.client_id == "by-appid"
    assert neither is not None and neither.client_id == "by-sub"


def test_a_rejection_reason_is_its_message() -> None:
    assert str(TokenRejected("wrong_audience", aud="x")) == "wrong_audience"


def test_an_unknown_tool_error_names_the_tool() -> None:
    with pytest.raises(UnknownToolError) as excinfo:
        Scopes.for_tool("not_a_tool")
    assert excinfo.value.args == ("not_a_tool",)


# --- the builders ------------------------------------------------------------
@pytest.mark.parametrize(
    ("issuer", "jwks_url", "message"),
    [
        (None, JWKS_URL, "set FLIGHTOPS_AUTH_ISSUER"),
        (ISSUER, None, "set FLIGHTOPS_AUTH_JWKS_URL"),
        (None, None, "set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL"),
    ],
)
def test_the_http_transport_refuses_to_start_and_names_exactly_what_is_missing(
    settings: Settings, issuer: str | None, jwks_url: str | None, message: str
) -> None:
    incomplete = settings.model_copy(update={"auth_issuer": issuer, "auth_jwks_url": jwks_url})
    with pytest.raises(AuthConfigError) as excinfo:
        build_resource_server_auth(incomplete)
    assert str(excinfo.value) == (
        f"the streamable-http transport requires authentication; {message}"
    )


async def test_the_resource_server_checks_the_configured_issuer(
    settings: Settings, clock: FakeClock
) -> None:
    built = build_resource_server_auth(
        configured(settings), jwks_transport=JwksEndpoint(KEY).transport, clock=clock
    )
    ours = KEY.mint(now=clock())
    theirs = KEY.mint(now=clock(), issuer="http://impostor.test")

    assert await built.verifier.verify_token(ours) is not None
    assert await built.verifier.verify_token(theirs) is None
    assert built.jwks.fetch_count == 1, "the verifier's key cache is the one handed back"


async def test_the_resource_server_uses_the_given_clock_and_skew(
    settings: Settings, clock: FakeClock
) -> None:
    built = build_resource_server_auth(
        configured(settings, auth_clock_skew_s=5.0),
        jwks_transport=JwksEndpoint(KEY).transport,
        clock=clock,
    )
    assert await built.verifier.verify_token(KEY.mint(now=clock(), nbf=int(clock()) + 5))
    assert await built.verifier.verify_token(KEY.mint(now=clock(), nbf=int(clock()) + 6)) is None


@pytest.mark.parametrize("builder", [build_resource_server_auth, build_approval_verifier])
async def test_the_key_cache_is_built_from_settings(settings: Settings, builder: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"keys": [KEY.jwk]})

    built = builder(
        configured(
            settings,
            user_agent="builder-test/1",
            http_timeout_s=3.5,
            jwks_cache_ttl_s=123.0,
            jwks_min_refresh_interval_s=7.0,
        ),
        jwks_transport=httpx.MockTransport(handler),
    )
    jwks = built.jwks if hasattr(built, "jwks") else built._verifier._jwks
    await jwks.key_for(KEY.kid)

    (request,) = seen
    assert request.headers["user-agent"] == "builder-test/1"
    assert request.extensions["timeout"]["read"] == 3.5
    # No clock reaches the cache from the builder, so its timings are read, not driven.
    assert (jwks._ttl_s, jwks._min_refresh_interval_s) == (123.0, 7.0)


@pytest.mark.parametrize(
    "update",
    [{"auth_issuer": None}, {"auth_jwks_url": None}, {"auth_issuer": None, "auth_jwks_url": None}],
)
def test_approvals_need_both_issuer_and_keys(settings: Settings, update: dict[str, Any]) -> None:
    assert build_approval_verifier(configured(settings, **update)).configured() is False
    assert build_approval_verifier(configured(settings)).configured() is True


async def test_the_approval_verifier_checks_the_issuer_age_and_clock_it_was_given(
    settings: Settings, clock: FakeClock
) -> None:
    approvals = build_approval_verifier(
        configured(settings, approval_max_age_s=60.0, approval_audience=AUDIENCE),
        jwks_transport=JwksEndpoint(APPROVAL_KEY).transport,
        clock=clock,
    )

    async def outcome(token: str) -> str:
        try:
            await approvals.verify(
                token, proposal_sha256=PROPOSAL.sha256, proposer=PROPOSER, digest=approved_digest
            )
        except ToolFailure as failure:
            return failure.code
        return "approved"

    token = grant(clock, expires_in=3600.0)
    assert await outcome(grant(clock, issuer="http://impostor.test")) == "approval_invalid"
    clock.advance(60)
    assert await outcome(token) == "approved"
    clock.advance(1)
    assert await outcome(token) == "approval_expired"


# --- approval ----------------------------------------------------------------
def approvals_for(clock: FakeClock, **kwargs: Any) -> ApprovalVerifier:
    jwks = JwksCache(JWKS_URL, transport=JwksEndpoint(APPROVAL_KEY).transport, clock=clock)
    check = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, clock=clock)
    return ApprovalVerifier(check, clock=clock, **kwargs)


async def refused(approvals: ApprovalVerifier, token: str, proposer: str = PROPOSER) -> str:
    with pytest.raises(ToolFailure) as excinfo:
        await approvals.verify(
            token, proposal_sha256=PROPOSAL.sha256, proposer=proposer, digest=approved_digest
        )
    return excinfo.value.code


@pytest.mark.parametrize(
    ("claims", "proposer", "advance", "code", "reason"),
    [
        (
            {"scp": "flightops.weather.read"},
            PROPOSER,
            0,
            "approver_not_authorised",
            "not_an_authorised_person",
        ),
        ({}, APPROVER, 0, "approver_is_proposer", "self_approval"),
        ({"now_offset": 600}, PROPOSER, 0, "approval_invalid", "issued_in_the_future"),
        ({"expires_in": 3600.0}, PROPOSER, 400.25, "approval_expired", "too_old"),
        ({"authorization_details": []}, PROPOSER, 0, "approval_invalid", "names_no_proposal"),
        (
            {"authorization_details": [detail({"v": 1, "from_flight": "SK1"})]},
            PROPOSER,
            0,
            "approval_payload_mismatch",
            "payload_mismatch",
        ),
    ],
)
async def test_every_refusal_leaves_an_audit_record_naming_the_approver(
    *,
    clock: FakeClock,
    logs: LogCapture,
    claims: dict[str, Any],
    proposer: str,
    advance: float,
    code: str,
    reason: str,
) -> None:
    # A copy: the parameter dict is shared by every run of this test in the process.
    claims = dict(claims)
    offset = claims.pop("now_offset", 0)
    token = grant(clock, now=clock() + offset, **claims)
    clock.advance(advance)

    assert await refused(approvals_for(clock), token, proposer) == code
    (event,) = logs.events("approval_rejected")
    assert event["reason"] == reason
    assert event["approver"] == APPROVER
    if reason == "too_old":
        assert event["age_s"] == 400.2
    assert token not in logs.text


async def test_a_grant_rejected_as_a_token_logs_the_verifiers_reason_and_context(
    clock: FakeClock, logs: LogCapture
) -> None:
    await refused(approvals_for(clock), grant(clock, audience=RESOURCE))
    (event,) = logs.events("approval_rejected")
    assert event["reason"] == "wrong_audience"
    assert event["expected"] == AUDIENCE


async def test_a_grant_issued_up_to_the_skew_ahead_is_accepted(clock: FakeClock) -> None:
    approvals = approvals_for(clock)
    at_edge = grant(clock, now=clock() + 30)
    past_edge = grant(clock, now=clock() + 31)
    approval = await approvals.verify(
        at_edge, proposal_sha256=PROPOSAL.sha256, proposer=PROPOSER, digest=approved_digest
    )
    assert approval.approved_by == APPROVER
    assert await refused(approvals, past_edge) == "approval_invalid"


@pytest.mark.parametrize("issued_at", ["1700000000", True, None])
async def test_a_grant_whose_issue_time_is_not_a_number_is_invalid(
    clock: FakeClock, issued_at: Any
) -> None:
    jwks = JwksCache(JWKS_URL, transport=JwksEndpoint(APPROVAL_KEY).transport, clock=clock)

    class Claims(JwtVerifier):
        async def verified_claims(self, token: str) -> dict[str, Any]:
            claims = await super().verified_claims(token)
            return {**claims, "iat": issued_at}

    approvals = ApprovalVerifier(
        Claims(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, clock=clock), clock=clock
    )
    assert await refused(approvals, grant(clock)) == "approval_invalid"


async def test_the_approval_verifier_uses_the_configured_clock_skew(
    settings: Settings, clock: FakeClock
) -> None:
    approvals = build_approval_verifier(
        configured(settings, auth_clock_skew_s=5.0, approval_audience=AUDIENCE),
        jwks_transport=JwksEndpoint(APPROVAL_KEY).transport,
        clock=clock,
    )
    await approvals.verify(
        grant(clock, nbf=int(clock()) + 5),
        proposal_sha256=PROPOSAL.sha256,
        proposer=PROPOSER,
        digest=approved_digest,
    )
    assert await refused(approvals, grant(clock, nbf=int(clock()) + 6)) == "approval_invalid"


async def test_the_default_age_limit_is_five_minutes(clock: FakeClock) -> None:
    approvals = approvals_for(clock)
    token = grant(clock, expires_in=3600.0)
    clock.advance(300)
    await approvals.verify(
        token, proposal_sha256=PROPOSAL.sha256, proposer=PROPOSER, digest=approved_digest
    )
    clock.advance(1)
    assert await refused(approvals, token) == "approval_expired"


# Constructors and their arguments, not failures: a failure built at import time is built
# before any mutant is switched on, so a test of it can never fail. The second mutation run
# showed exactly that (every `hint=None` survived a test that looked as if it checked hints).
REFUSALS: list[tuple[Any, tuple[Any, ...]]] = [
    (approval_module.approval_required, ("Because.",)),
    (approval_module.approval_declined, ("decline",)),
    (approval_module.approval_invalid, ()),
    (approval_module.approval_expired, ()),
    (approval_module.approval_payload_mismatch, ("a", "b")),
    (approval_module.approver_not_authorised, ()),
    (approval_module.approver_is_proposer, ()),
    (approval_module.approval_not_configured, ()),
]


@pytest.mark.parametrize(
    ("build", "args"), REFUSALS, ids=lambda item: getattr(item, "__name__", "")
)
def test_every_approval_refusal_says_what_happened_and_what_to_do(
    build: Any, args: tuple[Any, ...]
) -> None:
    """CLAUDE.md rule 3: code, message and hint, so the model can act on it."""
    failure: ToolFailure = build(*args)
    payload = failure.to_payload()["error"]
    assert payload["code"] == failure.code
    assert payload["message"]
    assert payload["hint"]


def test_a_declined_approval_says_how_it_was_answered() -> None:
    failure = approval_module.approval_declined("cancel")
    assert failure.details == {"action": "cancel"}
    assert "'cancel'" in failure.message


# --- the scope challenge -----------------------------------------------------
class App:
    """The inner ASGI app: records what reached it, and reads the body it is given."""

    def __init__(self) -> None:
        self.calls = 0
        self.body = b""

    async def __call__(self, _scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        while True:
            message = await receive()
            self.body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def user_with(*scopes: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        AccessToken(token="t", client_id="agent", scopes=list(scopes), resource=RESOURCE)
    )


def call(name: str, request_id: Any = 1, params: Any = None) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}} if params is None else params,
        }
    ).encode()


async def drive(
    body_messages: list[dict[str, Any]],
    *,
    method: str = "POST",
    path: str = "/mcp",
    kind: str = "http",
    user: Any = None,
) -> tuple[App, list[dict[str, Any]]]:
    app = App()
    middleware = ScopeChallengeMiddleware(
        app, path="/mcp", resource_metadata_url="http://127.0.0.1:8000/.well-known/x"
    )
    pending = list(body_messages)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": kind,
        "method": method,
        "path": path,
        "user": user_with() if user is None else user,
    }
    await middleware(scope, receive, send)
    return app, sent


def one(body: bytes) -> list[dict[str, Any]]:
    return [{"type": "http.request", "body": body, "more_body": False}]


async def test_a_refusal_is_a_403_with_a_complete_challenge() -> None:
    app, sent = await drive(one(call("airport_weather", request_id="req-9")))

    assert app.calls == 0
    start, body = sent
    headers = dict(start["headers"])
    assert start["status"] == 403
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"content-length"] == str(len(body["body"])).encode()
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"www-authenticate"] == (
        b'Bearer error="insufficient_scope", scope="flightops.weather.read", '
        b'resource_metadata="http://127.0.0.1:8000/.well-known/x", '
        b'error_description="The access token lacks a scope this tool requires"'
    )
    rpc = json.loads(body["body"])
    assert rpc["id"] == "req-9"
    assert rpc["error"]["code"] == INSUFFICIENT_SCOPE_RPC_CODE


@pytest.mark.parametrize("request_id", [True, 1.5, None, [1], {"a": 1}])
async def test_a_request_id_that_is_not_a_string_or_integer_is_answered_as_null(
    request_id: Any,
) -> None:
    _, sent = await drive(one(call("airport_weather", request_id=request_id)))
    assert json.loads(sent[1]["body"])["id"] is None


async def test_a_body_in_several_chunks_is_read_whole_and_replayed_whole() -> None:
    body = call("airport_weather")
    chunks = [body[:10], body[10:25], body[25:]]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < 2}
        for index, chunk in enumerate(chunks)
    ]
    _, refused_sent = await drive(messages)
    assert refused_sent[0]["status"] == 403, "the scope check saw the whole body"

    allowed, _ = await drive(
        [dict(message) for message in messages], user=user_with(Scopes.WEATHER_READ)
    )
    assert allowed.calls == 1
    assert allowed.body == body, "the app downstream gets every byte, in order"


async def test_a_final_message_need_not_say_more_body_false() -> None:
    """ASGI's default for `more_body` is False: reading on would wait for a message never sent."""
    _, sent = await drive([{"type": "http.request", "body": call("airport_weather")}])
    assert sent[0]["status"] == 403


async def test_the_size_limit_is_on_the_whole_body_not_each_chunk() -> None:
    # A call the token cannot make, one byte over the limit in total, in two chunks that
    # are each well under it. Only the running total can tell this from an inspectable body.
    body = call("airport_weather")
    body += b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE + 1 - len(body))
    half = len(body) // 2
    messages = [
        {"type": "http.request", "body": body[:half], "more_body": True},
        {"type": "http.request", "body": body[half:], "more_body": False},
    ]
    app, sent = await drive(messages)
    assert app.calls == 1, "over the limit in total: passed on uninspected"
    assert sent[0]["status"] == 200


async def test_a_message_without_a_body_key_is_an_empty_chunk() -> None:
    body = call("airport_weather")
    messages = [{"type": "http.request", "more_body": True}, *one(body)]
    _, sent = await drive(messages)
    assert sent[0]["status"] == 403


async def test_a_body_at_the_transport_limit_is_inspected_and_one_byte_more_is_not() -> None:
    body = call("airport_weather")
    padded = body + b" " * (DEFAULT_MAX_REQUEST_BODY_SIZE - len(body))
    _, at_limit = await drive(one(padded))
    assert at_limit[0]["status"] == 403

    over, sent = await drive(one(padded + b" "))
    assert over.calls == 1, "too large to inspect: passed on for the SDK to refuse"
    assert sent[0]["status"] == 200


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [("GET", "/mcp", "http"), ("POST", "/other", "http"), ("POST", "/mcp", "websocket")],
)
async def test_only_a_post_to_the_mcp_path_is_inspected(method: str, path: str, kind: str) -> None:
    app, sent = await drive(one(call("airport_weather")), method=method, path=path, kind=kind)
    assert app.calls == 1
    assert app.body == call("airport_weather"), "the app reads the original receive"
    assert sent[0]["status"] == 200


async def test_an_unauthenticated_request_is_left_to_the_sdk() -> None:
    app, _ = await drive(one(call("airport_weather")), user=object())
    assert app.calls == 1


@pytest.mark.parametrize("params", [["airport_weather"], "airport_weather", None, {"name": 5}])
async def test_a_call_whose_params_name_no_tool_is_passed_on(params: Any) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    app, _ = await drive(one(body.encode()))
    assert app.calls == 1


async def test_after_the_replayed_body_the_app_reads_from_the_socket() -> None:
    """A client that disconnects after the body: the app must see the disconnect."""
    app = App()
    middleware = ScopeChallengeMiddleware(app, path="/mcp", resource_metadata_url="x")
    received: list[str] = []
    pending = [*one(call("resolve_airport")), {"type": "http.disconnect"}]

    async def receive() -> dict[str, Any]:
        message = pending.pop(0)
        received.append(message["type"])
        return message

    async def reading_app(scope: Any, receive_: Any, send: Any) -> None:
        await receive_()
        received.append("app:" + (await receive_())["type"])

    middleware.app = reading_app
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "user": user_with(Scopes.REFERENCE_READ),
    }
    await middleware(scope, receive, lambda _message: None)
    assert received[-1] == "app:http.disconnect"


def test_the_challenge_needs_a_resource_server_url() -> None:
    server = ScopeChallengingServer("contract")
    with pytest.raises(RuntimeError, match="needs a resource server URL"):
        server.streamable_http_app()


async def test_the_challenge_guards_a_custom_mcp_path(settings: Settings) -> None:
    built = build_resource_server_auth(configured(settings))
    server = ScopeChallengingServer("contract", auth=built.settings, token_verifier=built.verifier)
    app = server.streamable_http_app(streamable_http_path="/custom")
    (installed,) = [m for m in app.user_middleware if m.cls is ScopeChallengeMiddleware]
    assert installed.kwargs["path"] == "/custom"

# Mutation survivors

The first 90 rows come from the survivor report of [run 44121537739](https://github.com/KristofferHM/flightops-mcp/actions/runs/44121537739). The rows for `dashboard/board.py` come from [run 44397823805](https://github.com/KristofferHM/flightops-mcp/actions/runs/44397823805). Ids are those of [run 44522301960](https://github.com/KristofferHM/flightops-mcp/actions/runs/44522301960), the latest read; its `mutation-report` artifact has every diff. mutmut renumbers mutants as code moves, so rows are matched by function and change, not by id.

## Run 44522301960: 111 survivors against a ceiling of 100

Counts in that run: killed: 2609, survived: 111. All 111 diffs were read.

| Survivors | Verdict |
|---:|---|
| 95 | Same id and same change as a row already judged here. Judgement unchanged. |
| 4 | Already judged, renumbered by the branch's new code: `x__weather_line__mutmut_35` is now `_54`, `x_triage_view__mutmut_92/98/99` are now `_102/108/109`. Judgement unchanged. |
| 12 | **Missing tests**, all new on this branch. Killed by the tests listed under "Killed in this round". |
| 0 | New and judged behaviour-identical. |

One row left the table without a test: `x__snapshot_epoch__mutmut_9` (`zero-epoch`). The check moved into
`_valid_epoch`, and its `0 <=` mutant is killed in this run.

Ceiling arithmetic: **was 100. 1 no longer survives (`zero-epoch`, above). 0 new survivors judged equivalent. 12 killed
by new tests. 100 − 1 + 12 − 12 = 99. Ceiling now 99.** The 99 is the 99 rows of the table below, each with a judgement.

### Killed in this round

| Mutant | Change | Test that now fails it |
|---|---|---|
| `x_triage_view__mutmut_137` | `count_rows(None)` for `count_rows(payload["counts"])` | `test_the_panel_passes_the_why_not_lines_through` |
| `x_triage_view__mutmut_147` | `weather_rows(None)` for `weather_rows(payload["weather"])` | same |
| `x_triage_view__mutmut_155` | `leg_rows(None)` | same |
| `x_triage_view__mutmut_156` | `payload.get(None)` for the legs | same |
| `x_triage_view__mutmut_157` | `payload.get("XXlegsXX")` | same |
| `x_triage_view__mutmut_158` | `payload.get("LEGS")` | same |
| `x_weather_rows__mutmut_82` | ceiling `basis` default `None` | `test_a_ceiling_without_a_basis_and_a_plain_visibility_read_as_given` |
| `x_weather_rows__mutmut_84` | ceiling `basis` default dropped (`None`) | same |
| `x_weather_rows__mutmut_87` | `"XXnot in the reportXX"` | same |
| `x_weather_rows__mutmut_88` | `"NOT IN THE REPORT"` | same |
| `x_weather_rows__mutmut_102` | `' or more'` always | same |
| `x_weather_rows__mutmut_108` | `'XXXX'` for `''` when the visibility is not a floor | same |

The six `triage_view` survivors were a **test that could not fail**, not only a missing one.
`test_the_panel_passes_the_why_not_lines_through` fed `weather: None` and `legs: {}` and checked only that there were
three count rows: exactly what `weather_rows(None)`, `leg_rows(None)` and `count_rows(None)` return. It was fixed, not
deleted: it now passes a real METAR, a real leg and the shared `COUNTS`, and pins the rows each produces. The
`weather_rows` survivors were a branch never run: every test gave the ceiling a `basis` and the visibility an
`at_least: True`. Each of the twelve was applied to `board.py` by hand and run against the board tests: all twelve
pass the old tests and fail the new ones.

## The judgements

Every surviving mutant is listed below with a judgement. None is a missing test: those were written (docs/QUALITY.md, section 3). Each category below is a judgement made by reading the mutants in it, not a filter applied to them.

| Judgement | Survivors | Why it is not a missing test |
|---|---|---|
| `prose` | 58 | Wording of a message, hint or exception text: wrapped in `XX` or re-cased. Equivalent for every caller: code branches on `code`, and the tests assert that a message and a hint exist (dropping either is killed). Pinning the exact words would be a change detector. |
| `header-case` | 8 | Case of an HTTP header name. Equivalent: header names are case-insensitive (RFC 9110, section 5.1) and httpx normalises them before sending. |
| `logger-name` | 4 | `get_logger(None)` for `get_logger(__name__)`. Equivalent: the logger name is not rendered into the JSON record and no per-logger level is configured, so the output is byte-identical. |
| `absent-claim-default` | 6 | The default for a claim that is absent from the token (`iss`/`tid` in the OBO cache key). Equivalent: any constant partitions identities the same way; every token without the claim gets the same value either way. The claim itself is killed when it is dropped. |
| `key-separator` | 1 | The separator joining scope names in the OBO cache key. Equivalent: any fixed separator yields the same partition, because scope names never contain whitespace (they are split on it everywhere they are read). |
| `unreachable-400` | 2 | `>= 400` became `> 400` or `>= 401` after the branch that handles 400, 401 and 403. Equivalent: a 400 never reaches this line. |
| `fast-path` | 2 | The lock-free read of the OBO cache was removed or keyed wrongly. Equivalent in result: the locked path re-reads the cache and returns the same token. It costs a lock acquisition, which no test should assert. |
| `rstrip-set` | 1 | `rstrip("/")` became `rstrip("XX/XX")`. `rstrip` takes a set of characters, so this also strips trailing `X`. Equivalent for every audience this server is configured with (URLs and `api://` URIs, none ending in `X`); judged acceptable rather than tested. |
| `pyjwk-alg` | 2 | `PyJWK(entry, algorithm=None)`. Equivalent: the entry was already required to declare `alg` RS256 or none, and PyJWK infers RS256 for an RSA key; the result is the same key. |
| `more-body-default` | 2 | `message.get("more_body", None)` for `False`. Equivalent: both are falsy. (`True` is killed by a test.) |
| `unconfigured-args` | 4 | An argument to the unconfigured `ApprovalVerifier(None, ...)`. Equivalent: that verifier raises `approval_not_configured` before it reads its clock or age limit. |
| `unknown-label` | 3 | The special case for the label `Unknown` was disabled or never matched. Equivalent: `"unknown".capitalize()` is `Unknown`, the same word. |
| `same-instant` | 2 | `datetime.fromtimestamp(snapshot)` without `UTC`. Equivalent: the naive local time goes straight into `astimezone(UTC)`, which reads it as local, so the instant and the text are unchanged. |
| `under-a-minute-tie` | 1 | `max(age, 1)` for `max(age, 0)` when ranking reports. Equivalent: it only ties ages of 1 s or less, and all of them print `Last update under 1 min ago` with the state `live`. |
| `whole-seconds` | 1 | `int(abs(seconds) / 60)` for `int(abs(seconds) // 60)`. Equivalent: both tools round ages to whole seconds, and for those truncating the quotient is the floor. |
| `truthy-count` | 1 | `sum(2 ...)` for `sum(1 ...)` in the banner's count of dated reports. Equivalent: the count is only tested for being non-zero. |
| `unreachable-cap` | 1 | `min(2.0, ...)` for `min(1.0, ...)` in `fade`. Equivalent: the other argument is `1.0` minus a non-negative number, so it never exceeds 1.0. |

## Every survivor

| Mutant | Judgement | Original | Mutated |
|---|---|---|---|
| `auth.approval.x_approval_declined__mutmut_11` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="XXDo not retry or rephrase the proposal to get a different answer. Report the XX"` |
| `auth.approval.x_approval_declined__mutmut_12` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="do not retry or rephrase the proposal to get a different answer. report the "` |
| `auth.approval.x_approval_declined__mutmut_13` | `prose` | `hint="Do not retry or rephrase the proposal to get a different answer. Report the "` | `hint="DO NOT RETRY OR REPHRASE THE PROPOSAL TO GET A DIFFERENT ANSWER. REPORT THE "` |
| `auth.approval.x_approval_declined__mutmut_14` | `prose` | `"outcome to the user.",` | `"XXoutcome to the user.XX",` |
| `auth.approval.x_approval_declined__mutmut_15` | `prose` | `"outcome to the user.",` | `"OUTCOME TO THE USER.",` |
| `auth.approval.x_approval_expired__mutmut_12` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="XXAsk the approver to approve the proposal again, then retry with the new grant.XX",` |
| `auth.approval.x_approval_expired__mutmut_13` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="ask the approver to approve the proposal again, then retry with the new grant.",` |
| `auth.approval.x_approval_expired__mutmut_14` | `prose` | `hint="Ask the approver to approve the proposal again, then retry with the new grant.",` | `hint="ASK THE APPROVER TO APPROVE THE PROPOSAL AGAIN, THEN RETRY WITH THE NEW GRANT.",` |
| `auth.approval.x_approval_invalid__mutmut_12` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="XXNot retryable with the same grant. A new approval must come from the approver; XX"` |
| `auth.approval.x_approval_invalid__mutmut_13` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="not retryable with the same grant. a new approval must come from the approver; "` |
| `auth.approval.x_approval_invalid__mutmut_14` | `prose` | `hint="Not retryable with the same grant. A new approval must come from the approver; "` | `hint="NOT RETRYABLE WITH THE SAME GRANT. A NEW APPROVAL MUST COME FROM THE APPROVER; "` |
| `auth.approval.x_approval_invalid__mutmut_15` | `prose` | `"it cannot be constructed.",` | `"XXit cannot be constructed.XX",` |
| `auth.approval.x_approval_invalid__mutmut_16` | `prose` | `"it cannot be constructed.",` | `"IT CANNOT BE CONSTRUCTED.",` |
| `auth.approval.x_approval_not_configured__mutmut_10` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="this server cannot verify approvals, so it will not write.",` |
| `auth.approval.x_approval_not_configured__mutmut_11` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="THIS SERVER CANNOT VERIFY APPROVALS, SO IT WILL NOT WRITE.",` |
| `auth.approval.x_approval_not_configured__mutmut_12` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="XXAn operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. XX"` |
| `auth.approval.x_approval_not_configured__mutmut_13` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="an operator must set flightops_auth_issuer and flightops_auth_jwks_url. "` |
| `auth.approval.x_approval_not_configured__mutmut_14` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "` | `hint="AN OPERATOR MUST SET FLIGHTOPS_AUTH_ISSUER AND FLIGHTOPS_AUTH_JWKS_URL. "` |
| `auth.approval.x_approval_not_configured__mutmut_15` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"XXNot retryable; the read tools are unaffected.XX",` |
| `auth.approval.x_approval_not_configured__mutmut_16` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"not retryable; the read tools are unaffected.",` |
| `auth.approval.x_approval_not_configured__mutmut_17` | `prose` | `"Not retryable; the read tools are unaffected.",` | `"NOT RETRYABLE; THE READ TOOLS ARE UNAFFECTED.",` |
| `auth.approval.x_approval_not_configured__mutmut_9` | `prose` | `message="This server cannot verify approvals, so it will not write.",` | `message="XXThis server cannot verify approvals, so it will not write.XX",` |
| `auth.approval.x_approval_payload_mismatch__mutmut_14` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="XXAn approval covers exactly the payload the approver saw. Propose again and have XX` |
| `auth.approval.x_approval_payload_mismatch__mutmut_15` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="an approval covers exactly the payload the approver saw. propose again and have "` |
| `auth.approval.x_approval_payload_mismatch__mutmut_16` | `prose` | `hint="An approval covers exactly the payload the approver saw. Propose again and have "` | `hint="AN APPROVAL COVERS EXACTLY THE PAYLOAD THE APPROVER SAW. PROPOSE AGAIN AND HAVE "` |
| `auth.approval.x_approval_payload_mismatch__mutmut_17` | `prose` | `"that proposal approved; do not reuse an approval across proposals.",` | `"XXthat proposal approved; do not reuse an approval across proposals.XX",` |
| `auth.approval.x_approval_payload_mismatch__mutmut_18` | `prose` | `"that proposal approved; do not reuse an approval across proposals.",` | `"THAT PROPOSAL APPROVED; DO NOT REUSE AN APPROVAL ACROSS PROPOSALS.",` |
| `auth.approval.x_approval_required__mutmut_10` | `prose` | `hint="You cannot approve this yourself. Tell the user that an approver must sign the "` | `hint="you cannot approve this yourself. tell the user that an approver must sign the "` |
| `auth.approval.x_approval_required__mutmut_12` | `prose` | `"proposal, and stop; do not retry without an approval.",` | `"XXproposal, and stop; do not retry without an approval.XX",` |
| `auth.approval.x_approval_required__mutmut_13` | `prose` | `"proposal, and stop; do not retry without an approval.",` | `"PROPOSAL, AND STOP; DO NOT RETRY WITHOUT AN APPROVAL.",` |
| `auth.approval.x_approval_required__mutmut_9` | `prose` | `hint="You cannot approve this yourself. Tell the user that an approver must sign the "` | `hint="XXYou cannot approve this yourself. Tell the user that an approver must sign the XX"` |
| `auth.approval.x_approver_is_proposer__mutmut_12` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="XXA second person must approve. Tell the user who needs to act; do not retry.XX",` |
| `auth.approval.x_approver_is_proposer__mutmut_13` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="a second person must approve. tell the user who needs to act; do not retry.",` |
| `auth.approval.x_approver_is_proposer__mutmut_14` | `prose` | `hint="A second person must approve. Tell the user who needs to act; do not retry.",` | `hint="A SECOND PERSON MUST APPROVE. TELL THE USER WHO NEEDS TO ACT; DO NOT RETRY.",` |
| `auth.approval.x_approver_not_authorised__mutmut_12` | `prose` | `"permission.",` | `"XXpermission.XX",` |
| `auth.approval.x_approver_not_authorised__mutmut_13` | `prose` | `"permission.",` | `"PERMISSION.",` |
| `auth.approval.x_approver_not_authorised__mutmut_14` | `prose` | `"Not retryable with the same approver.",` | `"XXNot retryable with the same approver.XX",` |
| `auth.approval.x_approver_not_authorised__mutmut_15` | `prose` | `"Not retryable with the same approver.",` | `"not retryable with the same approver.",` |
| `auth.approval.x_approver_not_authorised__mutmut_16` | `prose` | `"Not retryable with the same approver.",` | `"NOT RETRYABLE WITH THE SAME APPROVER.",` |
| `auth.challenge.x__read_body__mutmut_27` | `more-body-default` | `if not message.get("more_body", False):` | `if not message.get("more_body", None):` |
| `auth.challenge.x__read_body__mutmut_29` | `more-body-default` | `if not message.get("more_body", False):` | `if not message.get("more_body", ):` |
| `auth.challenge.xǁScopeChallengeMiddlewareǁ__init____mutmut_5` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.challenge.xǁScopeChallengingServerǁstreamable_http_app__mutmut_7` | `prose` | `raise RuntimeError("the scope challenge needs a resource server URL")` | `raise RuntimeError("XXthe scope challenge needs a resource server URLXX")` |
| `auth.jwks.x__parse_keys__mutmut_50` | `pyjwk-alg` | `key = jwt.PyJWK(entry, algorithm="RS256").key` | `key = jwt.PyJWK(entry, algorithm=None).key` |
| `auth.jwks.x__parse_keys__mutmut_52` | `pyjwk-alg` | `key = jwt.PyJWK(entry, algorithm="RS256").key` | `key = jwt.PyJWK(entry, ).key` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_15` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_26` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"user-agent": user_agent, "Accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_27` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"USER-AGENT": user_agent, "Accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_29` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"User-Agent": user_agent, "accept": "application/json"},` |
| `auth.jwks.xǁJwksCacheǁ__init____mutmut_30` | `header-case` | `headers={"User-Agent": user_agent, "Accept": "application/json"},` | `headers={"User-Agent": user_agent, "ACCEPT": "application/json"},` |
| `auth.obo.x__key__mutmut_10` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", "XXXX")),` |
| `auth.obo.x__key__mutmut_13` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", None)),` |
| `auth.obo.x__key__mutmut_15` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", )),` |
| `auth.obo.x__key__mutmut_18` | `absent-claim-default` | `str(claims.get("tid", "")),` | `str(claims.get("tid", "XXXX")),` |
| `auth.obo.x__key__mutmut_21` | `key-separator` | `" ".join(sorted(scopes)),` | `"XX XX".join(sorted(scopes)),` |
| `auth.obo.x__key__mutmut_5` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", None)),` |
| `auth.obo.x__key__mutmut_7` | `absent-claim-default` | `str(claims.get("iss", "")),` | `str(claims.get("iss", )),` |
| `auth.obo.x_obo_exchange_rejected__mutmut_11` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="XXThe identity provider refused to issue a downstream token for this user XX"` |
| `auth.obo.x_obo_exchange_rejected__mutmut_12` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="the identity provider refused to issue a downstream token for this user "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_13` | `prose` | `message="The identity provider refused to issue a downstream token for this user "` | `message="THE IDENTITY PROVIDER REFUSED TO ISSUE A DOWNSTREAM TOKEN FOR THIS USER "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_14` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="XXThe user's token is expired, revoked, not a user token, or lacks the scope. XX"` |
| `auth.obo.x_obo_exchange_rejected__mutmut_15` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="the user's token is expired, revoked, not a user token, or lacks the scope. "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_16` | `prose` | `hint="The user's token is expired, revoked, not a user token, or lacks the scope. "` | `hint="THE USER'S TOKEN IS EXPIRED, REVOKED, NOT A USER TOKEN, OR LACKS THE SCOPE. "` |
| `auth.obo.x_obo_exchange_rejected__mutmut_17` | `prose` | `"Ask the user to sign in again; do not retry with the same token.",` | `"XXAsk the user to sign in again; do not retry with the same token.XX",` |
| `auth.obo.x_obo_exchange_rejected__mutmut_18` | `prose` | `"Ask the user to sign in again; do not retry with the same token.",` | `"ask the user to sign in again; do not retry with the same token.",` |
| `auth.obo.x_obo_not_configured__mutmut_10` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="this server is not configured to call downstream apis on behalf of a user.",` |
| `auth.obo.x_obo_not_configured__mutmut_11` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="THIS SERVER IS NOT CONFIGURED TO CALL DOWNSTREAM APIS ON BEHALF OF A USER.",` |
| `auth.obo.x_obo_not_configured__mutmut_12` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="XXAn operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and XX"` |
| `auth.obo.x_obo_not_configured__mutmut_13` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="an operator must set flightops_auth_token_url, flightops_auth_client_id and "` |
| `auth.obo.x_obo_not_configured__mutmut_14` | `prose` | `hint="An operator must set FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID and "` | `hint="AN OPERATOR MUST SET FLIGHTOPS_AUTH_TOKEN_URL, FLIGHTOPS_AUTH_CLIENT_ID AND "` |
| `auth.obo.x_obo_not_configured__mutmut_15` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"XXFLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.XX",` |
| `auth.obo.x_obo_not_configured__mutmut_16` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"flightops_auth_client_secret. not retryable; the other tools are unaffected.",` |
| `auth.obo.x_obo_not_configured__mutmut_17` | `prose` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. Not retryable; the other tools are unaffected.",` | `"FLIGHTOPS_AUTH_CLIENT_SECRET. NOT RETRYABLE; THE OTHER TOOLS ARE UNAFFECTED.",` |
| `auth.obo.x_obo_not_configured__mutmut_9` | `prose` | `message="This server is not configured to call downstream APIs on behalf of a user.",` | `message="XXThis server is not configured to call downstream APIs on behalf of a user.XX",` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_15` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"user-agent": settings.user_agent, "Accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_16` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"USER-AGENT": settings.user_agent, "Accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_18` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"User-Agent": settings.user_agent, "accept": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_19` | `header-case` | `headers={"User-Agent": settings.user_agent, "Accept": "application/json"},` | `headers={"User-Agent": settings.user_agent, "ACCEPT": "application/json"},` |
| `auth.obo.xǁOboExchangerǁ__init____mutmut_6` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.obo.xǁOboExchangerǁ_exchange__mutmut_50` | `unreachable-400` | `if response.status_code >= 400:` | `if response.status_code > 400:` |
| `auth.obo.xǁOboExchangerǁ_exchange__mutmut_51` | `unreachable-400` | `if response.status_code >= 400:` | `if response.status_code >= 401:` |
| `auth.obo.xǁOboExchangerǁtoken_for__mutmut_10` | `fast-path` | `cached = self._cache.get(key)` | `cached = self._cache.get(None)` |
| `auth.obo.xǁOboExchangerǁtoken_for__mutmut_9` | `fast-path` | `cached = self._cache.get(key)` | `cached = None` |
| `auth.verifier.x__canonical__mutmut_3` | `rstrip-set` | `return audience.rstrip("/")` | `return audience.rstrip("XX/XX")` |
| `auth.verifier.xǁJwtVerifierǁ__init____mutmut_9` | `logger-name` | `self._log = get_logger(__name__)` | `self._log = get_logger(None)` |
| `auth.x_build_approval_verifier__mutmut_3` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=None, max_age_s=settings.approval_max_age_s)` |
| `auth.x_build_approval_verifier__mutmut_4` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=clock, max_age_s=None)` |
| `auth.x_build_approval_verifier__mutmut_6` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, max_age_s=settings.approval_max_age_s)` |
| `auth.x_build_approval_verifier__mutmut_7` | `unconfigured-args` | `return ApprovalVerifier(None, clock=clock, max_age_s=settings.approval_max_age_s)` | `return ApprovalVerifier(None, clock=clock, )` |
| `credits.xǁCreditLedgerǁ__init____mutmut_4` | `prose` | `raise ValueError("budget_per_hour must not be negative")` | `raise ValueError("XXbudget_per_hour must not be negativeXX")` |
| `dashboard.board.x__traffic_line__mutmut_5` | `same-instant` | `datetime.fromtimestamp(snapshot, UTC)` | `datetime.fromtimestamp(snapshot, None)` |
| `dashboard.board.x__traffic_line__mutmut_7` | `same-instant` | `datetime.fromtimestamp(snapshot, UTC)` | `datetime.fromtimestamp(snapshot, )` |
| `dashboard.board.x__weather_line__mutmut_54` | `under-a-minute-tie` | `(age < -METAR_STALE_AFTER_S, max(age, 0))` | `(age < -METAR_STALE_AFTER_S, max(age, 1))` |
| `dashboard.board.x_age_text__mutmut_3` | `whole-seconds` | `minutes = int(abs(seconds) // 60)` | `minutes = int(abs(seconds) / 60)` |
| `dashboard.board.x_banner__mutmut_12` | `truthy-count` | `dated = sum(1 for view in weather if view["age_s"] is not None)` | `dated = sum(2 for view in weather if view["age_s"] is not None)` |
| `dashboard.board.x_fade__mutmut_14` | `unreachable-cap` | `min(1.0, 1.0 - max(0.0, age_s) / FADE_AFTER_S)` | `min(2.0, 1.0 - max(0.0, age_s) / FADE_AFTER_S)` |
| `dashboard.board.x_triage_view__mutmut_102` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if (severity == "unknown") and False else severity.capitalize()` |
| `dashboard.board.x_triage_view__mutmut_108` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if severity == "XXunknownXX" else severity.capitalize()` |
| `dashboard.board.x_triage_view__mutmut_109` | `unknown-label` | `"Unknown" if severity == "unknown" else severity.capitalize()` | `"Unknown" if severity == "UNKNOWN" else severity.capitalize()` |

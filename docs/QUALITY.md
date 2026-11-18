# Quality gates

The suite is large. These gates ask whether it would catch a bug, and make the answer mechanical. Each one
runs in CI (`.github/workflows/ci.yml`) and fails the build on its own.

## 1. Coverage ratchet

Branch coverage, not line coverage: an `if` arm that never runs is where a bug waits.

```bash
uv run pytest --cov --cov-report=term
```

| | |
|---|---|
| Measured | `src/flightops_mcp`, `agent`, `evals`, `services`, `scripts` (everything that ships or runs; not `tests`) |
| Floor | `fail_under` in `pyproject.toml` |
| Enforced in | the `tests` job, on both Python versions |

**The floor only goes up.** It is the measured number rounded down, and it is raised in the same commit as
the tests that raise it. It is never raised by excluding code.

| Date | Measured | Floor | Commit |
|---|---|---|---|
| 2026-11-17 | 88.26 % | 88 | coverage ratchet introduced (v0.5.0 suite, no new tests) |
| 2026-11-17 | 88.84 % | 88 | property tests; still 88 rounded down |
| 2026-11-17 | 89.08 % | 89 | tests written from the first mutation run's survivors |
| 2026-11-18 | 90.42 % | 90 | tests written from the survivors of `dashboard/board.py` |

### The exclusion list

This is all of it. Adding to it needs a row here.

| Excluded | Where | Why it cannot be tested |
|---|---|---|
| `if __name__ == "__main__":` | `agent/__main__.py`, `evals/run.py`, `scripts/*.py`, `server.py` | The guard only; the `main()` it calls is tested |
| `if TYPE_CHECKING:` | none today | Never executes at runtime by definition |
| `# pragma: no cover` after a `for` that always returns | `http.py` (`request_json`), `opensky.py` (`_authorized_get`) | Unreachable: the loop body returns or raises on every path; the line satisfies the type checker |
| `# pragma: no cover` on `ToolFailure.__str__` | `errors.py` | A debugging aid; nothing in the server stringifies a failure |

`scripts/local_matrix.py` is measured and mostly uncovered here, because it runs against the compose stack in
the `compose-matrix` job rather than under pytest. It stays in the denominator: excluding it would raise the
number without testing anything.

## 2. Property tests

`hypothesis`, aimed at the code where a wrong answer is a wrong answer: arithmetic and parsers of what other
systems send. Each file states its properties in its docstring.

| File | Under test | The properties |
|---|---|---|
| `tests/test_properties_scopes.py` | `scopes_from_claims`, `Scopes.missing`, the IdP's exchange | a grant is exactly the whitespace-split, de-duplicated claim; case matters; a wrong-typed claim or role grants nothing; `missing` is required minus granted and ignores unknown or repeated grants; an exchanged token carries a non-empty subset of subject ∩ target, or nothing |
| `tests/test_properties_credits.py` | `CreditLedger`, `BBox.credit_cost` | against a model of the window: spend is never above the budget or below zero; a charge is refused exactly when it would cross the budget, the refusal is `local_budget_exhausted` with the numbers, and it changes nothing; a charge counts for one window including its edge; a bigger box never costs less |
| `tests/test_properties_http.py` | `parse_retry_after`, the credit header, `backoff_delay` | delta-seconds read as written; an HTTP-date is the time until it, or zero; blank, negative, `inf`, `nan`, `1_000` and anything else are absent, never zero and never negative; OpenSky's header wins in any case; backoff doubles, never shrinks, never passes the cap |
| `tests/test_properties_opensky.py` | `decode_states`, `decode_movements` | every row is decoded or counted (`received = decoded + unreadable`), so a count with unreadable rows is a lower bound; fields have their documented type or are unknown; truncation keeps the most recently heard; a body of the wrong shape is `upstream_unavailable`, not an empty sky |
| `tests/test_properties_airports.py` | `build_from_csv` | one airport per distinct ident, the later row winning; the return value is what is stored; quoted commas, quotes and line breaks round-trip; `limit` stops after that many accepted rows |

**Determinism.** Nothing here reads a clock: the ledger takes a `FakeClock`, the HTTP-date parser takes `now`.
Two profiles in `tests/conftest.py`: `dev` (100 examples, random, the default) and `ci` (500 examples,
derandomized, so a red build reproduces from the commit). Neither has a deadline: a failure because a disk was
slow is a timing assertion. CI sets `HYPOTHESIS_PROFILE=ci`.

### What they found

Each was a defect on `main` at `8fe3842`. The loader and `roles` rows were confirmed by running the new
properties against `main`'s modules (4 failures); the decoding and header rows by reading `main`'s code, since the
functions under test did not exist there in a testable form.

| Defect | Found by | Fix |
|---|---|---|
| `build_from_csv(limit=N)` checked the limit only every 5000 rows: `--limit 20` loaded the whole file | `test_a_limit_stops_after_that_many_accepted_rows` | checked per row |
| `build_from_csv` returned rows read, not airports stored: a repeated ident was counted twice | `test_one_airport_per_ident_and_the_count_says_so` | returns `count(*)` |
| `build_from_csv` never closed its connection (`with connect()` commits, does not close); on Windows the file could not be deleted for a rebuild | every loader property, on Windows | `contextlib.closing` |
| An elevation of `inf` raised `OverflowError` out of the loader; `nan` and `inf` coordinates were stored | the loader properties | non-finite numbers are unknown |
| A blank ident was stored as an airport called `""` | `test_one_airport_per_ident_and_the_count_says_so` | skipped |
| `StateVector.from_row` passed fields through untyped: a string `last_contact` made the most-recent-first sort raise `TypeError`, and `on_ground: "false"` was `True` | `test_decoded_fields_have_the_documented_type_or_are_unknown` | each field is its documented type or `None` |
| `"states": 0`, `""` or `{}` decoded as an empty sky | `test_a_states_field_that_is_not_a_list_is_unreadable_not_empty` | only `null` is empty |
| Two Retry-After parsers that disagreed: `opensky.py`'s ignored the HTTP-date form, and both read `inf`, `nan`, `1_000` and `-5` (`inf` then reached a JSON payload as `Infinity`; `-5` became "retry now") | `test_properties_http.py` | one strict parser in `http.py`, used by both |
| `int("1_000")` and `int("-3")` were read as credit balances | `test_a_credit_balance_is_never_negative` | digits only |
| `roles: [1, null, ""]` became the grants `"1"`, `"None"` and `""` | `test_a_role_that_is_not_one_scope_name_grants_nothing` | a role is one non-empty scope name |

Two refactors made the properties possible without an HTTP stack: the credit ledger moved out of `OpenSkyClient`
into `credits.py`, and response decoding into `decode_states` / `decode_movements`. Behaviour is unchanged
except as listed above.

## 3. Mutation testing

*If the code is broken, does a test fail?* Run on the code where a wrong answer is a security answer, a
money answer, or a stale board reported as live, and nowhere else.

| | |
|---|---|
| Tool | `mutmut` 3.8. It records which tests run each function and runs only those per mutant, in a forked process, so this scope takes minutes. `cosmic-ray` reruns the whole test command per mutant, which here would be hours. |
| Scope | `src/flightops_mcp/auth/*`, `src/flightops_mcp/credits.py`, `agent/approval.py`, `dashboard/board.py` |
| Tests | the 18 files in `[tool.mutmut]` in `pyproject.toml`, with the derandomized `mutation` hypothesis profile |
| Where | the `mutation` CI job, Linux only (mutmut forks per mutant; Windows has no fork) |
| Budget | `timeout 35m` on the run, 45 minutes on the job. Run 7 took 6 min 10 s. |
| Gate | `scripts/mutation_gate.py`: fails above `[tool.mutation-gate] ceiling`, on any mutant left unchecked (a run cut short is a lower bound, not a pass), and on zero mutants. A second step proves a ceiling one lower fails, over the same complete results. |
| Report | `docs/MUTATION-SURVIVORS.md`: every survivor, each judged. The CI artifact `mutation-report` has the diffs. |

`agent/approval.py` yields no mutants. It is two frozen dataclasses and a `Protocol` stub, with no function
body to mutate. The approval *logic* is `src/flightops_mcp/auth/approval.py`, which is in scope. The one
property of the agent module worth guarding, that the grant is kept out of `repr`, has a test
(`tests/test_agent_offline.py`).

### Runs

| Run | Mutants | Killed | Survived | What happened |
|---|---|---|---|---|
| 1 ([44107342465](https://github.com/KristofferHM/flightops-mcp/actions/runs/44107342465)) | 1324 | 932 (+1 timeout) | 391 | first read |
| 2 ([44116113793](https://github.com/KristofferHM/flightops-mcp/actions/runs/44116113793)) | 1324 | 0 | 0 | void: a new test mutated its own parameters and failed mutmut's second clean run; the gate refused 1324 unchecked mutants as a pass |
| 3 ([44117501997](https://github.com/KristofferHM/flightops-mcp/actions/runs/44117501997)) | 1324 | 1196 | 128 | after the first round of tests |
| 4 ([44121537739](https://github.com/KristofferHM/flightops-mcp/actions/runs/44121537739)) | 1324 | 1234 | **90** | after the second round; the ceiling is set here |
| 5 ([44392875173](https://github.com/KristofferHM/flightops-mcp/actions/runs/44392875173)) | 2293 | 1938 | 355 | `dashboard/board.py` added: 969 mutants, 265 survivors, the other 90 unchanged |
| 6 ([44395175602](https://github.com/KristofferHM/flightops-mcp/actions/runs/44395175602)) | 2293 | 2188 | 105 | after `tests/test_board_views.py` |
| 7 ([44397823805](https://github.com/KristofferHM/flightops-mcp/actions/runs/44397823805)) | 2293 | 2193 | **100** | five more missing tests; the ceiling is 100 |

### What the self-test costs

The step `A ceiling one lower fails the gate` never re-ran mutmut. It judges the results of the real run again
with the ceiling lowered by one. Its cost was one `mutmut show` process per survivor, for diffs the verdict does
not use. It now reads no diffs, and the report step reads them four at a time. It stays on the complete results
rather than a sample: a separate run over one module would add mutation time and prove less.

Measured on 2026-11-18, from the step timestamps GitHub records (whole seconds).

| | Before ([44373065479](https://github.com/KristofferHM/flightops-mcp/actions/runs/44373065479)) | After ([44397823805](https://github.com/KristofferHM/flightops-mcp/actions/runs/44397823805)) |
|---|---|---|
| Mutants | 1324 | 2293 |
| Survivors reported | 90 | 100 |
| `Mutate` | 8 min 51 s | 6 min 10 s |
| `Survivors against the ceiling` | 44 s | 15 s |
| `A ceiling one lower fails the gate` | 44 s | under 1 s |
| Whole job | 10 min 30 s | 6 min 43 s |

Only the two gate rows are the effect of this change, 88 s down to 15 s. `Mutate` varies by runner: the same
1324 mutants took between 5 min 34 s and 8 min 51 s across the five runs before this one, so the difference in
that row and in the job total is not a saving to claim.

The assertion was shown to fire. In [run 44397867084](https://github.com/KristofferHM/flightops-mcp/actions/runs/44397867084)
a temporary commit (`ebff2c7`, since reverted) made the ceiling check off by one. The gate step passed, and the
self-test failed the job with `the gate passed with the ceiling at 99`.

### What the survivors were

Each one was either a missing test or a test that could not fail. Both kinds were fixed. None was deleted or
weakened.

**Missing tests, the ones that matter:**
- **The issuer check, through the builders.** `build_resource_server_auth` and `build_approval_verifier`
  both survived passing `issuer=None` to the verifier, which turns PyJWT's issuer check off. No test had
  built a verifier from `Settings` and shown it a token from another issuer.
- **The JWKS client's passthrough guard.** `event_hooks=None` survived. The guard was asserted only by
  scanning the source text (`test_every_outbound_http_client_installs_the_guard`), and mutmut's
  instrumented copy defeats a text scan. It is now asserted by making a request.
- **JWKS parsing.** `continue` → `break` survived, so one malformed entry would silently drop every key after
  it. The `kty`/`use`/`alg` filters each survived as well.
- **The OBO cache.** It survived losing the issuer from its key. A held lock could be pruned (`and` → `or`),
  which allows a second concurrent exchange for one identity. All users could share one lock, so one slow
  exchange stalled everyone. The last two now have tests with a gated transport.
- **The scope challenge.** The size limit could be applied per chunk instead of to the whole body. A final
  message without `more_body` was not covered. The `no-store` and `content-length` headers were unasserted.
- **The verifier's clock.** Nothing showed that PyJWT's own clock checks are off (`verify_nbf`/`verify_iat`
  could be switched on), so the suite was quietly relying on the wall clock and the injected one agreeing.
- **The audit trail.** `approval_rejected` was asserted for 2 of its 7 reasons.
- **The credit ledger's window edge.** `< cutoff` → `<= cutoff` survived because a random float never lands
  exactly on the edge. It is now an explicit `@example`.

**Tests that could not fail, the interesting kind:**
- `test_a_rejection_logs_its_reason_and_never_the_grant` asserted `approved_sha256 != PROPOSAL.sha256`. That
  is also true when the field is logged as `None`. It now asserts the digest of what the grant showed.
- The refusal-shape tests built their `ToolFailure`s at import time, before mutmut switches a mutant on, so
  they passed whatever the code did. Every `hint=None` survived them. They now build the failure inside the
  test. The general lesson: an input computed at collection time is not exercised by the code under test.
- A new test popped from its parametrized dict, which is shared by every run in a process. It passed once
  and failed on mutmut's second clean run (run 2). It now copies the dict, and the whole selection is
  checked to pass twice in one interpreter.

**The board's display rules, the same two lessons again.** 255 of the 265 first survivors in
`dashboard/board.py` were killed by tests. Most were keys and words no test pinned, because the tests checked
fragments of each view. Several were wrong answers waiting to happen: the two-hour boundary in `age_text`, the
future-tolerance boundary in the badge, an undated METAR outranking a dated one, and a dropped conversion to UTC
that cannot show on a UTC host. There, unlike in the refusals below, the words are the behaviour, so
`tests/test_board_views.py` asserts whole views.

### The 100 that remain

All of them are judged in `docs/MUTATION-SURVIVORS.md`. 58 are prose: the wording or case of a message or
hint. A test asserts that every refusal *has* a message and a hint; pinning the words would make a test
that breaks on every edit. The other 42 are behaviour-identical: header-name case, which HTTP ignores;
logger names, which are not rendered; defaults for absent claims; a `>= 400` after the branch that took
400; the cache fast path that the locked path repeats; the ten in `dashboard/board.py`; and the rest listed
there. The ceiling is 100: the earlier 90 plus those ten. It goes down when one of these is killed, and it
does not go up without a row in that file.

## 4. Secret and dependency scans

The `scans` job. Each step fails the build on a finding.

| Scan | What it covers | Result on this branch |
|---|---|---|
| `gitleaks dir .` (8.30.1, checksum-verified) | the checked-out tree, before any install | clean |
| `gitleaks git . --log-opts=--all` | every commit on every ref (`fetch-depth: 0`) | clean: 35 commits scanned on run 44123397766 (merges add no content and are not counted), one verified false positive, named below |
| `pip-audit` | every package in `uv.lock`, all dependency groups, hashes required | no known vulnerabilities |

`.gitleaksignore` is the allowlist, and every entry in it names the line and why it is not a secret. There is
one: `tests/test_rebooking_proposal.py:45`, which asserts the idempotency key `rbk_` + the first 32 hex digits
of the SHA-256 of a public test proposal. The full digest is asserted on line 37 of the same file. It is
listed twice, once for the tree and once for the commit that introduced it.

Run locally, the tree scan also reports `.env` (gitignored, never committed: the developer's generated
compose secrets) and `.venv`. Neither is in a checkout, which is what CI scans.

## 5. Accessibility

The `a11y` job runs `node scripts/a11y.mjs`: axe-core and Playwright, pinned in `scripts/package-lock.json`, over
the real board on synthetic tool answers. `docs/DASHBOARD.md` lists what it walks and what fails it.

A browser is worth having here because contrast, focus and sideways overflow only exist once a page is laid out.
It costs 31 s of job time in run 44397823805, on a job that runs beside the others. The first version installed
the browser's system packages too and took 6 min 47 s, 5 min 18 s of it in `apt`. The runner image already has
what the headless shell needs.

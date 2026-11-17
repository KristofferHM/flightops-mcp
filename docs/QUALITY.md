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

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

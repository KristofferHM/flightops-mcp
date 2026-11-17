# Security pass (Phase 5)

What was checked on 2026-11-17, how, and what it found, including where it found nothing. The scans
that Phase 4b put in CI (`gitleaks` over the tree and the whole history, `pip-audit` over every locked
dependency) are not repeated here; this page covers what was still missing: an image scan, a review
of how every dependency is pinned, and a manual secrets audit of the history.

## 1. Image scan

**What runs.** The `container` job builds the shipped image (`Dockerfile`), smoke-tests it, then
scans it with `trivy image --scanners vuln`. One scan, every severity, kept as the job's artifact
(`image-scan`: `trivy.json`, `trivy.txt`) and summarised in the run. The gate fails the job on any
**fixable HIGH or CRITICAL** finding. A finding with no fixed version is reported but not gated:
nothing in this repository can act on it, and a gate that is always red stops being read. A last
check plants one fixable HIGH in a copy of the report and requires the gate to count it, so the
gate itself is shown able to fail.

**How trivy is installed.** As the release binary, not through `aquasecurity/trivy-action` or
`setup-trivy`. On 2026-03-19 a compromised maintainer credential was used to publish a malicious
Trivy v0.69.4 and to force-push 76 of trivy-action's 77 tags to a credential stealer
(CVE-2026-33634, GHSA-69fq-xp46-6x23). The job pins **v0.69.3**, which NVD names as known safe, and
checks the download against the release's checksum file before it runs.

**Open.** The checksum file comes from the same release as the binary, so it protects against a
corrupt download, not against a replaced release. The hash should be pinned in the workflow, as
gitleaks' already is. It was not pinned in this pass because the fetch from this host was refused;
the job prints the hash on every run, so pinning it is one line once a run has been read.

**Result.** Stated in `ROADMAP.md` with the CI run that produced it (the scan cannot run on this
host: no Docker).

**Base image.** The runtime stage now runs `apt-get upgrade` before anything else, so a Debian
security fix published after `python:3.12-slim` was last rebuilt is in the image. Without it, the
gate would fail on findings that are fixed in Debian but not yet in the base tag.

## 2. Dependency pinning review

| What | How it is pinned | Verdict |
|---|---|---|
| Python packages (104, all groups) | `uv.lock`, exact versions, **1411 sha256 hashes**; every install in CI and both Dockerfiles uses `--frozen` | Pinned and verified. A changed artifact fails the install. |
| Direct dependency ranges in `pyproject.toml` | `>=` floors, except `mcp[cli]==2.3.0` | Right for a lockfile project: the lock decides, the ranges say what is allowed. The SDK is exact on purpose: its auth surface moves, and a bump is a change to `auth/` (ROADMAP risk register). |
| Build backend | `uv_build>=0.11.19,<0.12.0` | Bounded to a minor. |
| Interpreter | `requires-python >=3.12`, `.python-version` 3.12, CI on 3.12 and 3.13 | Pinned for development; the range is tested. |
| uv | `0.11.19` in CI (`UV_VERSION`) and in both Dockerfiles (`ghcr.io/astral-sh/uv:0.11.19`) | Version-pinned, not digest-pinned. |
| Base image | `python:3.12-slim`, by tag | **Floating by design.** A digest pin is reproducible and also never receives a security fix unless something bumps it, and this repository has no bot to do that. The tag plus `apt-get upgrade` plus a gated scan on every push is the trade made. Revisit when Renovate or Dependabot is added. |
| Observability images | `jaegertracing/all-in-one:1.62`, `prom/prometheus:v3.0.1`, `grafana/grafana:11.3.0`, overridable | Pinned by tag, and **old**: these were current a long time ago and are not scanned. They run only under the `observability` profile, bound to 127.0.0.1, and are never part of a deployment. Recorded as debt, not fixed here. |
| GitHub Actions | **Now pinned to commit SHAs** (`actions/checkout`, `astral-sh/setup-uv`, `actions/upload-artifact`), the tag in a comment | Changed in this pass. Before, all three were by major tag, which is what the trivy-action attack used. Resolved with `git ls-remote` on 2026-11-17; `setup-uv`'s `v5` is an annotated tag, so the peeled commit is used. |
| CI tools fetched at run time | gitleaks 8.30.1 by version **and sha256**; trivy 0.69.3 by version and the release's checksum file | gitleaks fully pinned; trivy as above (open item). |

`pip-audit` over every locked dependency in every group, run locally on 2026-11-17:
**No known vulnerabilities found.** CI runs the same command on every push (`scans` job).

## 3. Secrets audit of the history

`gitleaks` already scans every commit on every ref in CI. This is the manual sanity check the
roadmap asks for (`git log -p | grep -i token`), run on 2026-11-17 over **45 commits, every ref**.

The literal command is not useful in this repository: `git log -p --all | grep -ci token` matches
**1940 lines**, because the project is about tokens. So the check was narrowed to the shapes a real
credential has, over every added line in the history:

| Looked for | Found | Verdict |
|---|---:|---|
| JWTs (`eyJ…`.`…`.`…`) | 0 | none |
| Private key blocks (`-----BEGIN … PRIVATE KEY-----`) | 0 | none; the IdP generates its key at start-up |
| AWS, GitHub, Slack, Google, OpenAI, Anthropic key prefixes | 0 | none |
| `Bearer` followed by a 20+ character literal | 2 | `CANARY-BEARER-xyz789` (the redaction canary) and `test-key-not-a-credential` (a test fixture); both by design |
| A secret-named key assigned a 16+ character literal | 5 | test fixtures (`servers-own-token`, `revoked-at-the-vendor`) and shell references to generated variables (`"$SCHIPHOL_SECRET"`); no literal secret |
| Quoted 32+ hex or base64 literals | 61 | 58 lines of scenario digests in `evals/baseline.json` across its versions, the pinned proposal digest in `tests/test_rebooking_proposal.py`, the W3C example trace id in `tests/test_otel.py`, gitleaks' release checksum in `ci.yml` |
| `.env` files ever committed | 1 | `.env.example` only, placeholders |

**Result: no findings.** Nothing in the history is a credential, and nothing needed rotating. That
agrees with the gitleaks history scan, whose one finding is the verified false positive named in
`.gitleaksignore`.

This branch adds no secret. The mock upstream refuses every client unless its secret arrives by
environment; `scripts/load_test.sh` generates secrets per run; CI generates the mock's secret into
`.env` like the other two.

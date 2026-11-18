# ADR 0006: Pin what can be rewritten, scan what cannot be pinned

- Status: accepted
- Date: 2026-11-17
- Phase: 5 (`v1.0.0` candidate)

## Context

Every input to a build is either content-addressed (it cannot change without its name changing) or
not (a tag, a version range, a URL). The repository already had both kinds:

- Python packages: `uv.lock` with 1411 sha256 hashes, installed `--frozen` everywhere. Content-addressed.
- gitleaks: a version and a sha256. Content-addressed.
- GitHub Actions, the base image, uv's image, trivy: tags or versions. Not content-addressed.

In March 2026 an attacker with a maintainer's credential force-pushed 76 of `aquasecurity/trivy-action`'s
77 tags to a credential stealer and published a malicious Trivy v0.69.4 (CVE-2026-33634). Anyone pinned
to a tag ran it, and so did anyone who adopted a version the day it appeared. A security scanner was
the attack vector.

## Decision

1. **Anything a third party can rewrite and we cannot vet on every change is pinned to content.**
   GitHub Actions are pinned to commit SHAs, with the tag in a comment. Release binaries fetched in CI
   are pinned by version and verified by hash.
2. **Security tools are not taken from the newest release, nor from an action.** Trivy is the v0.69.3
   binary, a version NVD names as known safe, pinned by sha256. The hash was taken from the first CI
   run, where it matched the release's checksum file: trust on first use (docs/SECURITY.md).
3. **The base image is deliberately not pinned to a digest.** A digest pin never receives a security
   fix unless something bumps it, and this repository has no Renovate or Dependabot to do that. The
   trade instead: `python:3.12-slim` by tag, `apt-get upgrade` at build, and an image scan on every
   push that fails on a fixable HIGH or CRITICAL finding. Reproducibility is given up; patch latency
   is not.
4. **Unfixed findings are reported, not gated.** A gate on findings nobody can fix is always red, and
   a gate that is always red is not read.
5. **Every gate shows it can fail**: the image gate is run once more on a report with a planted
   fixable finding, as the mutation and eval gates already do.

## Consequences

- Updating an action is a deliberate edit (resolve the new tag, check the commit), not something that
  happens to the build.
- The image changes when Debian publishes a fix, so two builds of the same commit can differ. The
  scan record of each run is kept as an artifact.
- The first run of the image gate failed on a real finding, a fixable zlib heap overflow
  (CVE-2026-85091), because the upgrade step had not reached that commit. That is what a gate is for
  (docs/SECURITY.md).
- The optional observability images (Jaeger, Prometheus, Grafana) are pinned by old tags and not
  scanned. They are localhost-only and never deployed; recorded as debt.

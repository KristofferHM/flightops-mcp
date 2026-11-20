"""A manifest of what a mutation cache is a baseline *for*.

The cache mutmut writes is keyed by the source a mutant was cut from, so a fork that copies it
re-runs every mutant whose line changed — correct, but silent: it cannot tell a reader whether the
files it skipped are still the files that produced the results.

This writes the other half: the sha256 of every mutated file and of every test in the selection,
at the moment the cache was made. A consumer compares its own tree against it and refuses the cache
when they differ, rather than skipping mutants this repository never judged.

    uv run python scripts/mutation_baseline.py --manifest out.json
    uv run python scripts/mutation_baseline.py --check out.json   # exit 1 on a mismatch

`--check` compares only the paths the manifest lists that also exist here: a fork adds files, which
is the point of forking, and their absence is not a mismatch. A file in both that differs is.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "flightops.mutations.baseline/v1"


def _config() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return cast(dict[str, Any], tomllib.load(handle)["tool"]["mutmut"])


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files() -> tuple[list[Path], list[Path]]:
    """The mutated sources (globs expanded, sorted) and the selected tests, as configured."""
    config = _config()
    sources: list[Path] = []
    for pattern in cast(list[str], config.get("only_mutate", [])):
        matched = sorted(Path(p) for p in glob.glob(str(ROOT / pattern)))
        sources.extend(matched)
    selected = cast(list[str], config.get("pytest_add_cli_args_test_selection", []))
    tests = [ROOT / entry for entry in selected]
    return sources, tests


def manifest() -> dict[str, Any]:
    sources, tests = _files()
    return {
        "schema": SCHEMA,
        "sources": {_rel(p): _digest(p) for p in sources if p.is_file()},
        "tests": {_rel(p): _digest(p) for p in tests if p.is_file()},
    }


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def check(path: Path) -> int:
    recorded = json.loads(path.read_text(encoding="utf-8"))
    if recorded.get("schema") != SCHEMA:
        print(f"FAIL: {path} is not {SCHEMA}")
        return 1
    problems: list[str] = []
    for section in ("sources", "tests"):
        for name, digest in recorded.get(section, {}).items():
            local = ROOT / name
            if not local.is_file():
                continue  # a fork may not carry a file; that is not a mismatch
            if _digest(local) != digest:
                problems.append(f"{section}: {name}")
    if problems:
        print("FAIL: the cache was made against different files:")
        for problem in problems:
            print(f"  {problem}")
        print("Re-run mutation without seeding the cache, or re-seed from a run of this tree.")
        return 1
    sources = cast(dict[str, str], recorded.get("sources", {}))
    tests = cast(dict[str, str], recorded.get("tests", {}))
    print(f"OK: every file the manifest lists matches ({len(sources)} sources, {len(tests)} tests)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="write the manifest here")
    parser.add_argument(
        "--check", type=Path, metavar="PATH", help="compare this tree against a manifest"
    )
    args = parser.parse_args(argv)
    if args.check:
        return check(args.check)
    data = manifest()
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.manifest:
        args.manifest.write_text(text, encoding="utf-8")
        print(f"wrote {args.manifest}: {len(data['sources'])} sources, {len(data['tests'])} tests")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

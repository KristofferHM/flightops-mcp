"""The mutation baseline manifest: what a cached run is a baseline *for*.

The point of the manifest is to make a seeded cache refusable. A fork that copies mutmut's cache
should skip only the mutants this repository actually judged, against the same source and the same
tests; when either has moved, the honest answer is to re-run rather than to inherit a verdict.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import mutation_baseline as baseline


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.mutmut]\n"
        'only_mutate = ["lib/*.py"]\n'
        'pytest_add_cli_args_test_selection = ["tests/test_lib.py"]\n',
        encoding="utf-8",
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "one.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "lib" / "two.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lib.py").write_text("def test_one(): pass\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "ROOT", tmp_path)
    return tmp_path


def test_the_manifest_names_every_mutated_source_and_selected_test(tree: Path) -> None:
    data = baseline.manifest()
    assert data["schema"] == baseline.SCHEMA
    assert set(data["sources"]) == {"lib/one.py", "lib/two.py"}
    assert set(data["tests"]) == {"tests/test_lib.py"}


def test_a_matching_tree_passes_its_own_manifest(tree: Path, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline.manifest()), encoding="utf-8")
    assert baseline.check(path) == 0


def test_a_changed_source_is_a_mismatch(tree: Path, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline.manifest()), encoding="utf-8")
    (tree / "lib" / "one.py").write_text("x = 2\n", encoding="utf-8")
    assert baseline.check(path) == 1


def test_a_changed_test_is_a_mismatch(tree: Path, tmp_path: Path) -> None:
    # Stronger than the source case: inheriting a kill from a test that has since been weakened is
    # exactly the failure the manifest exists to prevent.
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline.manifest()), encoding="utf-8")
    (tree / "tests" / "test_lib.py").write_text("def test_one(): assert True\n", encoding="utf-8")
    assert baseline.check(path) == 1


def test_a_file_the_fork_does_not_carry_is_not_a_mismatch(tree: Path, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline.manifest()), encoding="utf-8")
    (tree / "lib" / "two.py").unlink()
    assert baseline.check(path) == 0


def test_a_manifest_from_another_schema_is_refused(tree: Path, tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"schema": "something/else"}), encoding="utf-8")
    assert baseline.check(path) == 1

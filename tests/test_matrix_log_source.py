"""The matrix reads the services' own logs. An unreadable one must stop the run."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "local_matrix.py"


@pytest.fixture
def matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """The script imports as a module, with the environment it needs to import at all."""
    log = tmp_path / "service.log"
    log.write_text("started\n", encoding="utf-8")
    monkeypatch.setenv("MATRIX_IDP_LOG", str(log))
    monkeypatch.setenv("MATRIX_SERVER_LOG", str(log))
    monkeypatch.setenv("DEV_AGENT_CLIENT_SECRET", "canary-not-a-secret")
    monkeypatch.setenv("DEV_MCP_CLIENT_SECRET", "canary-not-a-secret")
    spec = importlib.util.spec_from_file_location("local_matrix_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_a_file_source_reads_the_file(matrix: ModuleType, tmp_path: Path) -> None:
    log = tmp_path / "idp.log"
    log.write_text("POST /token\n", encoding="utf-8")
    source = matrix.LogSource(path=log)
    assert source.read() == "POST /token\n"


def test_a_command_source_reads_what_the_command_prints(matrix: ModuleType) -> None:
    source = matrix.LogSource(command=(sys.executable, "-c", "print('from the service')"))
    assert source.read().strip() == "from the service"


def test_a_failing_command_stops_the_run(matrix: ModuleType) -> None:
    source = matrix.LogSource(command=(sys.executable, "-c", "raise SystemExit(3)"))
    with pytest.raises(SystemExit) as stopped:
        source.read()
    assert "log command failed (3)" in str(stopped.value)


def test_file_is_the_default_and_a_command_replaces_it(
    matrix: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("X_LOG", str(tmp_path / "x.log"))
    monkeypatch.delenv("X_LOG_CMD", raising=False)
    assert matrix.log_source("X_LOG", "X_LOG_CMD").path == tmp_path / "x.log"

    monkeypatch.delenv("X_LOG")
    monkeypatch.setenv("X_LOG_CMD", "docker compose logs --no-color idp")
    assert matrix.log_source("X_LOG", "X_LOG_CMD").command == (
        "docker",
        "compose",
        "logs",
        "--no-color",
        "idp",
    )


@pytest.mark.parametrize("both", [True, False])
def test_exactly_one_source_is_required(
    matrix: ModuleType, monkeypatch: pytest.MonkeyPatch, both: bool
) -> None:
    monkeypatch.delenv("X_LOG", raising=False)
    monkeypatch.delenv("X_LOG_CMD", raising=False)
    if both:
        monkeypatch.setenv("X_LOG", "a.log")
        monkeypatch.setenv("X_LOG_CMD", "cat a.log")
    with pytest.raises(SystemExit, match="exactly one"):
        matrix.log_source("X_LOG", "X_LOG_CMD")

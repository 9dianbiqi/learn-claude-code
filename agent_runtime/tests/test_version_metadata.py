from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime import __version__
from agent_runtime.cli import build_parser, main


EXPECTED_RELEASE_VERSION = "0.3.0.dev3"
CHANGELOG = Path(__file__).parents[1] / "CHANGELOG.md"


def test_release_version_matches_package_and_changelog() -> None:
    assert __version__ == EXPECTED_RELEASE_VERSION
    assert CHANGELOG.read_text(encoding="utf-8").startswith(
        f"# Changelog\n\n## v{__version__} "
    )


def test_cli_version_flag_matches_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"agent-runtime {__version__}"


def test_doctor_runtime_version_matches_package_version(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", "120")

    assert main(["doctor", "--repo", str(tmp_path), "--minimum-free-mb", "0"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["runtime_version"] == __version__

import json
import shutil
from argparse import ArgumentParser
from pathlib import Path

import pytest

from requirementseeker_dataset.cli import build_parser, main

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "local-test-secret-at-least-32-bytes"


def test_cli_reads_secret_by_environment_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "raw" / "valid", raw)
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)

    result = main(
        [
            "sanitize",
            "--raw",
            str(raw),
            "--plan",
            str(FIXTURES / "approved-manifest.json"),
            "--output",
            str(tmp_path / "sanitized"),
            "--secret-env",
            "RS_DATASET_TEST_SECRET",
        ]
    )
    output = capsys.readouterr().out

    assert result == 0
    assert SECRET not in output
    assert json.loads(output)["sampling_manifest_count"] == 1


def _all_options(parser: ArgumentParser) -> set[str]:
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for subparser in choices.values():
                options.update(_all_options(subparser))
    return options


def test_cli_accepts_only_secret_environment_name() -> None:
    options = _all_options(build_parser())

    assert "--secret" not in options
    assert "--secret-env" in options


def test_cli_exports_and_validates_blank_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = tmp_path / "raw"
    shutil.copytree(FIXTURES / "raw" / "valid", raw)
    monkeypatch.setenv("RS_DATASET_TEST_SECRET", SECRET)
    sanitized = tmp_path / "sanitized"
    labels = tmp_path / "labels"
    assert (
        main(
            [
                "sanitize",
                "--raw",
                str(raw),
                "--plan",
                str(FIXTURES / "approved-manifest.json"),
                "--output",
                str(sanitized),
                "--secret-env",
                "RS_DATASET_TEST_SECRET",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["export-labels", "--sanitized", str(sanitized), "--output", str(labels)]) == 0
    export_summary = json.loads(capsys.readouterr().out)
    assert export_summary["annotation_count"] == 1
    assert export_summary["comment_count"] == 2

    assert main(["validate-labels", str(labels)]) == 0
    validation_summary = json.loads(capsys.readouterr().out)
    assert validation_summary["annotation_count"] == 1
    assert validation_summary["evaluation_eligible_count"] == 0
    assert validation_summary["status"] == "ok"


def test_cli_reports_only_safe_error_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "raw-private-marker"
    invalid = tmp_path / marker
    invalid.mkdir()

    assert main(["validate-labels", str(invalid)]) == 2
    output = capsys.readouterr().out
    assert marker not in output
    assert json.loads(output) == {"error": "annotation_files_missing", "status": "error"}

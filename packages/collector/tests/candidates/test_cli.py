from __future__ import annotations

import json
from pathlib import Path

import pytest

import requirementseeker_collector.cli as cli
from requirementseeker_collector.candidates.discovery import DiscoveryRequest, DiscoveryResult
from requirementseeker_collector.cli import main

DIRECTIONS = (
    ["software_tools"] * 6
    + ["tutorial_workflow"] * 6
    + ["life_services"] * 4
    + ["entertainment_culture"] * 4
    + ["ecommerce_marketing"] * 4
)
SCALES = ["up_to_200", "201_to_2000", "over_2000"] * 8


def _write_manifest(path: Path, *, complete: bool) -> Path:
    videos: list[dict[str, str]] = []
    for index, (direction, scale) in enumerate(zip(DIRECTIONS, SCALES, strict=True)):
        platform = "bilibili" if index % 2 == 0 else "douyin"
        key = f"video-{index + 1}"
        host = "www.bilibili.com" if platform == "bilibili" else "www.douyin.com"
        videos.append(
            {
                "platform": platform,
                "video_key": key,
                "url": f"https://{host}/video/{key}",
                "direction": direction,
                "comment_scale": scale,
            }
        )
    if not complete:
        videos.pop()
    path.write_text(
        json.dumps(
            {
                "manifest_version": "1.0",
                "videos": videos,
                "unavailable_platforms": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _summary(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.isascii()
    return json.loads(captured.out)


@pytest.mark.parametrize(
    "source_arguments",
    [[], ["--query", "AI", "--source-url", "https://search.bilibili.com/all"]],
)
def test_discover_requires_exactly_one_source(
    source_arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "discover",
                "--platform",
                "bilibili",
                "--direction",
                "software_tools",
                *source_arguments,
            ]
        )
        == 2
    )
    assert _summary(capsys) == {"status": "invalid_request"}


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-results", "0"],
        ["--max-results", "501"],
        ["--max-pages", "0"],
        ["--max-pages", "21"],
    ],
)
def test_discover_rejects_out_of_bounds_caps(
    arguments: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: pytest.fail("must not discover"))

    assert (
        main(
            [
                "discover",
                "--platform",
                "bilibili",
                "--direction",
                "software_tools",
                "--query",
                "AI",
                *arguments,
            ]
        )
        == 2
    )
    assert _summary(capsys) == {"status": "invalid_request"}


def test_discover_uses_default_caps_and_ephemeral_chromium(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    received: list[tuple[DiscoveryRequest, Path, object]] = []

    def fake_discover(
        request: DiscoveryRequest, *, output_root: Path, launch_config: object
    ) -> DiscoveryResult:
        received.append((request, output_root, launch_config))
        directory = output_root / "candidates" / "run-id"
        return DiscoveryResult([], 2, directory / "manifest.json", directory / "discovery.json")

    monkeypatch.setattr(cli, "discover", fake_discover)

    assert (
        main(
            [
                "discover",
                "--platform",
                "bilibili",
                "--direction",
                "software_tools",
                "--query",
                "AI tools",
            ]
        )
        == 0
    )

    request, output_root, launch_config = received[0]
    assert request.max_results == 50
    assert request.max_pages == 3
    assert output_root == (tmp_path / ".local-data/m2-real").resolve()
    assert launch_config.browser == "chromium"
    assert launch_config.session_mode == "ephemeral"
    assert _summary(capsys) == {
        "candidate_count": 0,
        "discovery_path": str(output_root / "candidates/run-id/discovery.json"),
        "manifest_path": str(output_root / "candidates/run-id/manifest.json"),
        "pages_processed": 2,
        "status": "success",
    }


def test_discover_wires_explicit_dedicated_browser(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    received: list[object] = []

    def fake_discover(
        request: DiscoveryRequest, *, output_root: Path, launch_config: object
    ) -> DiscoveryResult:
        del request
        received.append(launch_config)
        directory = output_root / "candidates" / "run-id"
        return DiscoveryResult([], 1, directory / "manifest.json", directory / "discovery.json")

    monkeypatch.setattr(cli, "discover", fake_discover)

    assert (
        main(
            [
                "discover",
                "--platform",
                "douyin",
                "--direction",
                "life_services",
                "--query",
                "AI",
                "--browser",
                "edge",
                "--reuse-login",
            ]
        )
        == 0
    )
    launch_config = received[0]
    assert launch_config.browser == "edge"
    assert launch_config.platform == "douyin"
    assert launch_config.session_mode == "dedicated"
    assert _summary(capsys)["status"] == "success"


def test_validate_plan_returns_nonzero_for_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path / "manifest.json", complete=False)

    assert main(["validate-plan", str(path)]) == 1

    summary = _summary(capsys)
    assert summary["valid"] is False
    assert "video_total" in [gap["code"] for gap in summary["gaps"]]


def test_validate_plan_returns_zero_only_for_complete_coverage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path / "manifest.json", complete=True)

    assert main(["validate-plan", str(path)]) == 0

    assert _summary(capsys) == {"gaps": [], "suggestions": [], "valid": True}


def test_discover_does_not_expose_automatic_batch_flag() -> None:
    parser = cli._parser()
    help_text = parser.format_help()
    subparsers = next(
        action for action in parser._actions if isinstance(action, cli.argparse._SubParsersAction)
    )
    help_text += "\n".join(command.format_help() for command in subparsers.choices.values())

    assert "--auto-batch" not in help_text

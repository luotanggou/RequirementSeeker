from __future__ import annotations

import json
from pathlib import Path

import pytest

import requirementseeker_collector.cli as cli
from requirementseeker_collector.cli import main
from requirementseeker_collector.contracts import ManifestVideo
from requirementseeker_collector.runner import PilotRequest, PilotResult


class FakeLiveCollector:
    def __init__(self) -> None:
        self.pilot_calls: list[tuple[PilotRequest, Path]] = []
        self.batch_calls: list[tuple[str, str, Path]] = []

    def collect_pilot(self, request: PilotRequest, output_root: Path) -> PilotResult:
        self.pilot_calls.append((request, output_root))
        return PilotResult(request.platform, request.video_key or "derived", "success", 2, 2)

    def collect(self, item: ManifestVideo, output_root: Path) -> PilotResult:
        self.batch_calls.append((item.platform, item.video_key, output_root))
        return PilotResult(item.platform, item.video_key, "success", 2, 2)


def output(capsys: pytest.CaptureFixture[str]) -> tuple[str, dict[str, object]]:
    captured = capsys.readouterr()
    return captured.err, json.loads(captured.out)


def write_manifest(path: Path, videos: list[dict[str, str]], version: str = "1.0") -> None:
    path.write_text(
        json.dumps(
            {
                "manifest_version": version,
                "videos": videos,
                "unavailable_platforms": [],
            }
        ),
        encoding="utf-8",
    )


def manifest_item(platform: str, video_key: str) -> dict[str, str]:
    host = "www.bilibili.com" if platform == "bilibili" else "www.douyin.com"
    return {
        "platform": platform,
        "video_key": video_key,
        "url": f"https://{host}/video/{video_key}",
        "direction": "software_tools",
        "comment_scale": "up_to_200",
    }


def test_cli_rejects_source_url_credentials_without_echo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "pilot",
                "--platform",
                "bilibili",
                "--url",
                "https://user:pass@example.invalid/video",
            ]
        )
        == 2
    )
    stderr, summary = output(capsys)
    assert "pass" not in json.dumps(summary) + stderr
    assert summary == {"status": "invalid_request"}


def test_pilot_uses_default_local_output_root_and_emits_compact_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeLiveCollector()
    monkeypatch.setattr(cli, "BrowserVideoCollector", lambda: fake)

    exit_code = main(
        [
            "pilot",
            "--platform",
            "bilibili",
            "--url",
            "https://www.bilibili.com/video/BVfake",
            "--video-key",
            "BVfake",
        ]
    )

    stderr, summary = output(capsys)
    assert exit_code == 0
    assert stderr == ""
    assert fake.pilot_calls[0][1] == (tmp_path / ".local-data/m2-real").resolve()
    assert summary == {
        "collected_total": 2,
        "platform": "bilibili",
        "status": "success",
        "target": 2,
        "video_key": "BVfake",
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--browser", "chrome"],
        ["--browser", "edge"],
        ["--browser", "chromium", "--reuse-login"],
    ],
)
def test_cli_rejects_invalid_browser_mode_before_collector_creation(
    arguments: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda *args, **kwargs: pytest.fail("collector must not be created"),
    )

    exit_code = main(
        [
            "pilot",
            "--platform",
            "bilibili",
            "--url",
            "https://www.bilibili.com/video/BVfake",
            *arguments,
        ]
    )

    assert exit_code == 2
    assert output(capsys)[1] == {"status": "invalid_request"}


def test_cli_wires_explicit_dedicated_browser_to_pilot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeLiveCollector()
    received: list[dict[str, object]] = []

    def collector(**kwargs: object) -> FakeLiveCollector:
        received.append(kwargs)
        return fake

    monkeypatch.setattr(cli, "BrowserVideoCollector", collector)

    exit_code = main(
        [
            "pilot",
            "--platform",
            "bilibili",
            "--url",
            "https://www.bilibili.com/video/BVfake",
            "--browser",
            "chrome",
            "--reuse-login",
        ]
    )

    assert exit_code == 0
    assert received == [{"browser": "chrome", "reuse_login": True}]
    assert output(capsys)[1]["status"] == "success"


def test_pilot_rejects_url_for_other_platform_without_opening_browser(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda: pytest.fail("browser must not be started for invalid input"),
    )

    assert (
        main(
            [
                "pilot",
                "--platform",
                "douyin",
                "--url",
                "https://www.bilibili.com/video/BVfake",
            ]
        )
        == 2
    )
    _, summary = output(capsys)
    assert summary == {"status": "invalid_request"}


@pytest.mark.parametrize(
    ("content", "status"),
    [
        ("not-json secret-marker", "manifest_invalid"),
        ('{"manifest_version":"2.0","videos":[],"unavailable_platforms":[]}', "manifest_invalid"),
        ('{"manifest_version":"1.0","videos":[],"unavailable_platforms":[]}', "manifest_empty"),
    ],
)
def test_batch_rejects_invalid_or_empty_manifest_without_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
    status: str,
) -> None:
    path = tmp_path / "secret-marker.json"
    path.write_text(content, encoding="utf-8")

    assert main(["batch", str(path)]) == 2

    stderr, summary = output(capsys)
    assert summary == {"status": status}
    assert "secret-marker" not in json.dumps(summary) + stderr


def test_batch_rejects_duplicate_video_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "manifest.json"
    item = manifest_item("bilibili", "BV1")
    write_manifest(path, [item, item])

    assert main(["batch", str(path)]) == 2
    _, summary = output(capsys)
    assert summary == {"status": "manifest_invalid"}


def test_batch_rejects_duplicate_casefolded_output_paths_before_opening_browser(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "manifest.json"
    write_manifest(
        path,
        [manifest_item("bilibili", "BVsame"), manifest_item("bilibili", "bvsame")],
    )
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda: pytest.fail("collector must not start for duplicate output paths"),
    )

    assert main(["batch", str(path)]) == 2
    _, summary = output(capsys)
    assert summary == {"status": "manifest_invalid"}


def test_batch_processes_manifest_in_file_order_and_emits_platform_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "manifest.json"
    write_manifest(
        path,
        [
            manifest_item("douyin", "DY2"),
            manifest_item("bilibili", "BV1"),
            manifest_item("douyin", "DY1"),
        ],
    )
    fake = FakeLiveCollector()
    monkeypatch.setattr(cli, "BrowserVideoCollector", lambda: fake)

    output_root = tmp_path / ".local-data/m2-real/batch-one"
    assert main(["batch", str(path), "--output-root", str(output_root)]) == 0

    stderr, summary = output(capsys)
    assert stderr == ""
    assert [(platform, key) for platform, key, _ in fake.batch_calls] == [
        ("douyin", "DY2"),
        ("bilibili", "BV1"),
        ("douyin", "DY1"),
    ]
    assert summary["status"] == "success"
    assert summary["videos_succeeded"] == 3
    assert summary["platforms"] == {
        "bilibili": {"status": "completed", "videos_succeeded": 1},
        "douyin": {"status": "completed", "videos_succeeded": 2},
    }


def test_batch_wires_explicit_dedicated_browser(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "manifest.json"
    write_manifest(path, [manifest_item("bilibili", "BV1")])
    fake = FakeLiveCollector()
    received: list[dict[str, object]] = []

    def collector(**kwargs: object) -> FakeLiveCollector:
        received.append(kwargs)
        return fake

    monkeypatch.setattr(cli, "BrowserVideoCollector", collector)

    assert main(["batch", str(path), "--browser", "edge", "--reuse-login"]) == 0

    assert received == [{"browser": "edge", "reuse_login": True}]
    assert output(capsys)[1]["status"] == "success"


def test_pilot_rejects_redirected_profile_before_collector_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output_root = tmp_path / ".local-data" / "m2-real"
    outside = tmp_path / "outside"
    outside.mkdir()
    profile_root = output_root / "browser-profiles"
    profile_root.parent.mkdir(parents=True)
    try:
        profile_root.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda *args, **kwargs: pytest.fail("collector must not be created"),
    )

    assert (
        main(
            [
                "pilot",
                "--platform",
                "bilibili",
                "--url",
                "https://www.bilibili.com/video/BVfake",
                "--browser",
                "chrome",
                "--reuse-login",
            ]
        )
        == 2
    )

    assert output(capsys)[1] == {"status": "invalid_request"}


@pytest.mark.parametrize(
    "unsafe",
    [
        ".",
        "..",
        ".local-data",
        ".local-data/m2-real/../../escape",
        ".local-data/m2-real/CON",
        ".local-data/m2-real/name.",
        ".local-data/m2-real/child:stream",
        "\\\\server\\share\\output",
        "\\\\?\\C:\\output",
        "\\\\.\\C:\\output",
    ],
)
def test_output_root_rejects_paths_outside_the_local_data_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    monkeypatch.chdir(tmp_path)

    assert cli._output_root(unsafe) is None


def test_output_root_returns_a_canonical_boundary_or_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    boundary = tmp_path / ".local-data/m2-real"

    assert cli._output_root(".local-data/m2-real") == boundary.resolve()
    assert cli._output_root(".local-data/m2-real/./nested") == (boundary / "nested").resolve()


def test_output_root_rejects_a_symbolic_link_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    boundary = tmp_path / ".local-data/m2-real"
    boundary.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = boundary / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        original_resolve = Path.resolve

        def resolve_escape(path: Path, strict: bool = False) -> Path:
            if path == Path(".local-data/m2-real/escape") or path == link:
                return outside.resolve()
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve_escape)

    assert cli._output_root(".local-data/m2-real/escape") is None


def test_cli_rejects_empty_output_root(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda: pytest.fail("collector must not start for invalid output root"),
    )

    assert (
        main(
            [
                "pilot",
                "--platform",
                "bilibili",
                "--url",
                "https://www.bilibili.com/video/BVfake",
                "--output-root",
                "",
            ]
        )
        == 2
    )
    _, summary = output(capsys)
    assert summary == {"status": "invalid_request"}


def test_cli_rejects_unsafe_video_key_before_opening_browser(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "BrowserVideoCollector",
        lambda: pytest.fail("collector must not start for an unsafe output key"),
    )

    assert (
        main(
            [
                "pilot",
                "--platform",
                "bilibili",
                "--url",
                "https://www.bilibili.com/video/BVfake",
                "--video-key",
                "../escape",
            ]
        )
        == 2
    )
    _, summary = output(capsys)
    assert summary == {"status": "invalid_request"}

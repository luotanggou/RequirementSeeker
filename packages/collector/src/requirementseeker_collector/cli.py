"""Safe compact-JSON CLI for collector pilot and batch runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Never, cast

from pydantic import ValidationError

from .browser import BrowserName
from .contracts import CollectionManifest
from .runner import (
    BrowserVideoCollector,
    PilotRequest,
    _path_is_safe_under,
    browser_profile_path,
    manifest_paths_are_safe,
    run_batch,
    video_key_is_safe,
)

_MAX_MANIFEST_BYTES = 10 * 1024 * 1024


class _ArgumentError(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise _ArgumentError("invalid_arguments")


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Collect public video comments in a visible browser")
    commands = parser.add_subparsers(dest="command", required=True)

    pilot = commands.add_parser("pilot", help="Collect one public video")
    pilot.add_argument("--platform", required=True)
    pilot.add_argument("--url", required=True)
    pilot.add_argument("--video-key")
    pilot.add_argument("--output-root", default=".local-data/m2-real")
    pilot.add_argument("--browser", choices=("chromium", "chrome", "edge"), default="chromium")
    pilot.add_argument("--reuse-login", action="store_true")

    batch = commands.add_parser("batch", help="Collect a versioned manifest in file order")
    batch.add_argument("manifest")
    batch.add_argument("--output-root", default=".local-data/m2-real")
    batch.add_argument("--browser", choices=("chromium", "chrome", "edge"), default="chromium")
    batch.add_argument("--reuse-login", action="store_true")
    return parser


def _output_root(raw: object) -> Path | None:
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or "\x00" in raw
        or raw.startswith(("\\\\", "//"))
    ):
        return None
    try:
        workspace = Path.cwd().resolve(strict=True)
        lexical_boundary = (workspace / ".local-data" / "m2-real").absolute()
        lexical_candidate = Path(raw).absolute()
        lexical_relative = lexical_candidate.relative_to(lexical_boundary)
        if any(not video_key_is_safe(part) for part in lexical_relative.parts):
            return None
        if not _path_is_safe_under(workspace, lexical_candidate):
            return None
        boundary = lexical_boundary.resolve(strict=False)
        candidate = lexical_candidate.resolve(strict=False)
        boundary.relative_to(workspace)
        relative = candidate.relative_to(boundary)
    except (OSError, RuntimeError, ValueError):
        return None
    if any(not video_key_is_safe(part) for part in relative.parts):
        return None
    return candidate


def _read_manifest(path: Path) -> CollectionManifest | None:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_MANIFEST_BYTES:
            return None
        return CollectionManifest.model_validate_json(raw)
    except (OSError, ValidationError, ValueError):
        return None


def _browser_mode_is_valid(browser: str, reuse_login: bool) -> bool:
    return (browser == "chromium" and not reuse_login) or (
        browser in {"chrome", "edge"} and reuse_login
    )


def _collector(browser: str, reuse_login: bool) -> BrowserVideoCollector:
    if browser == "chromium":
        return BrowserVideoCollector()
    return BrowserVideoCollector(browser=cast(BrowserName, browser), reuse_login=reuse_login)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _ArgumentError:
        _emit({"status": "invalid_request"})
        return 2

    output_root = _output_root(args.output_root)
    if output_root is None:
        _emit({"status": "invalid_request"})
        return 2
    if not _browser_mode_is_valid(args.browser, args.reuse_login):
        _emit({"status": "invalid_request"})
        return 2

    if args.command == "pilot":
        try:
            request = PilotRequest.model_validate(
                {"platform": args.platform, "url": args.url, "video_key": args.video_key}
            )
        except ValidationError:
            _emit({"status": "invalid_request"})
            return 2
        if request.video_key is not None and not video_key_is_safe(request.video_key):
            _emit({"status": "invalid_request"})
            return 2
        if (
            args.reuse_login
            and browser_profile_path(output_root, request.platform, args.browser) is None
        ):
            _emit({"status": "invalid_request"})
            return 2
        try:
            result = _collector(args.browser, args.reuse_login).collect_pilot(request, output_root)
        except Exception:
            _emit({"status": "collection_failed"})
            return 1
        _emit(result.to_summary())
        return 0 if result.status == "success" else 1

    manifest = _read_manifest(Path(args.manifest))
    if manifest is None:
        _emit({"status": "manifest_invalid"})
        return 2
    if not manifest.videos:
        _emit({"status": "manifest_empty"})
        return 2
    if not manifest_paths_are_safe(manifest):
        _emit({"status": "manifest_invalid"})
        return 2
    if args.reuse_login and any(
        browser_profile_path(output_root, platform, args.browser) is None
        for platform in {item.platform for item in manifest.videos}
    ):
        _emit({"status": "invalid_request"})
        return 2
    try:
        batch_result = run_batch(manifest, _collector(args.browser, args.reuse_login), output_root)
    except Exception:
        _emit({"status": "collection_failed"})
        return 1
    _emit(batch_result.to_summary())
    return 0 if batch_result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

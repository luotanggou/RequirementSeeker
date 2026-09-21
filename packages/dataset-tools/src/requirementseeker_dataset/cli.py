"""`rs-dataset` 的安全命令行入口，只输出 ASCII 摘要和固定错误码。"""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .identifiers import SecretConfigurationError
from .labels import LabelValidationError, export_labels, validate_label_root
from .sanitize import SanitizationError, sanitize_root
from .source import RawDatasetError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rs-dataset")
    commands = parser.add_subparsers(dest="command", required=True)

    sanitize = commands.add_parser("sanitize", help="sanitize approved raw collections")
    sanitize.add_argument("--raw", type=Path, required=True)
    sanitize.add_argument("--plan", type=Path, required=True)
    sanitize.add_argument("--output", type=Path, required=True)
    sanitize.add_argument("--secret-env", required=True, metavar="NAME")

    export = commands.add_parser("export-labels", help="create blank human label files")
    export.add_argument("--sanitized", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate-labels", help="validate labels and adjudications")
    validate.add_argument("label_root", type=Path)
    return parser


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _error_code(error: Exception) -> str:
    value = str(error).split(":", maxsplit=1)[0]
    return value if value and value.replace("_", "").isalnum() else "operation_failed"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sanitize":
            sanitization = sanitize_root(args.raw, args.plan, args.output, args.secret_env)
            _emit(
                {
                    "excluded_raw_directory_count": sanitization.excluded_raw_directory_count,
                    "file_count": len(sanitization.output_files),
                    "output": str(args.output.resolve(strict=False)),
                    "sampling_manifest_count": len(sanitization.sampling_manifests),
                    "sanitization_report_count": len(sanitization.sanitization_reports),
                    "status": "ok",
                }
            )
        elif args.command == "export-labels":
            export = export_labels(args.sanitized, args.output)
            _emit(
                {
                    "annotation_count": len(export.annotations),
                    "comment_count": len(export.comments),
                    "output": str(args.output.resolve(strict=False)),
                    "status": "ok",
                }
            )
        else:
            validation = validate_label_root(args.label_root)
            _emit(
                {
                    "annotation_count": validation.annotation_count,
                    "evaluation_eligible_count": validation.evaluation_eligible_count,
                    "label_root": str(args.label_root.resolve(strict=False)),
                    "status": "ok",
                }
            )
    except (
        LabelValidationError,
        RawDatasetError,
        SanitizationError,
        SecretConfigurationError,
    ) as error:
        _emit({"error": _error_code(error), "status": "error"})
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - console script delegates here.
    raise SystemExit(main())

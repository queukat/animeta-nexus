from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path.cwd()


def _strip_remainder_separator(values: Sequence[str]) -> list[str]:
    args = list(values)
    if args and args[0] == "--":
        return args[1:]
    return args


def _command_center(args: argparse.Namespace) -> int:
    from .metadata_command_center import COMMAND_CENTER_REPORT, generate

    report = generate(refresh_demo_data=args.refresh_demo_data)
    try:
        report_path = COMMAND_CENTER_REPORT.relative_to(PROJECT_ROOT)
    except ValueError:
        report_path = COMMAND_CENTER_REPORT
    if args.json:
        print(json.dumps(report["metrics"], indent=2, ensure_ascii=False))
    else:
        print("Metadata Command Center regenerated.")
        print(f"Report: {report_path}")
        print(f"Demo records: {report['metrics']['demo_corpus_episodes']}")
        print("Live site: https://queukat.github.io/animeta-nexus/")
    return 0


def _doctor(_: argparse.Namespace) -> int:
    from .metadata_command_center import COMMAND_CENTER_REPORT

    report = json.loads(COMMAND_CENTER_REPORT.read_text(encoding="utf-8")) if COMMAND_CENTER_REPORT.exists() else {}
    checks = [
        ("command_center_report", COMMAND_CENTER_REPORT.exists()),
        ("demo_records", int(report.get("metrics", {}).get("demo_corpus_episodes", 0)) > 0),
        ("docs_index", (PROJECT_ROOT / "docs" / "index.html").exists()),
        ("pyproject", (PROJECT_ROOT / "pyproject.toml").exists()),
        ("no_env_file", not (PROJECT_ROOT / ".env").exists()),
    ]

    forbidden_patterns = [
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
        re.compile(r"C:\\Users\\", re.IGNORECASE),
    ]
    public_paths = [
        PROJECT_ROOT / "README.md",
        PROJECT_ROOT / "docs",
        PROJECT_ROOT / "animeta_nexus",
        PROJECT_ROOT / "scripts",
    ]
    leaks: list[str] = []
    for path in public_paths:
        if path.is_file():
            candidates = [path]
        elif path.exists():
            candidates = [item for item in path.rglob("*") if item.is_file()]
        else:
            candidates = []

        for item in candidates:
            if (
                ".git" in item.parts
                or "__pycache__" in item.parts
                or item.suffix == ".pyc"
                or item.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}
                or any(part.endswith(".egg-info") for part in item.parts)
            ):
                continue
            text = item.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in forbidden_patterns):
                leaks.append(str(item.relative_to(PROJECT_ROOT)))

    checks.append(("secret_scan", not leaks))

    width = max(len(name) for name, _ in checks)
    ok = True
    for name, passed in checks:
        ok = ok and passed
        state = "PASS" if passed else "FAIL"
        print(f"{name.ljust(width)}  {state}")
    if leaks:
        print("Potential leaks:")
        for leak in leaks:
            print(f"  {leak}")
    return 0 if ok else 1


def _reconstruct(args: argparse.Namespace) -> int:
    from .metadata_reconstruction_core import main as reconstruct_main

    reconstruct_main(_strip_remainder_separator(args.reconstruction_args))
    return 0


def _push(args: argparse.Namespace) -> int:
    from .tvdb_contribution_rail import main as push_main

    push_main(_strip_remainder_separator(args.push_args))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="animeta",
        description="AniMeta Nexus command surface for metadata recovery operations.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    command_center = subparsers.add_parser(
        "command-center",
        aliases=["demo"],
        help="Regenerate the static Metadata Command Center showcase.",
    )
    command_center.add_argument(
        "--refresh-demo-data",
        action="store_true",
        help="Rewrite the deterministic demo corpus before generating the Command Center.",
    )
    command_center.add_argument("--json", action="store_true", help="Print metrics as JSON.")
    command_center.set_defaults(func=_command_center)

    doctor = subparsers.add_parser("doctor", help="Run local safety and showcase checks.")
    doctor.set_defaults(func=_doctor)

    reconstruct = subparsers.add_parser(
        "reconstruct",
        help="Forward arguments to the Metadata Reconstruction Core.",
        usage="animeta reconstruct [core options]",
        add_help=False,
    )
    reconstruct.add_argument("reconstruction_args", nargs=argparse.REMAINDER)
    reconstruct.set_defaults(func=_reconstruct)

    push = subparsers.add_parser(
        "push",
        help="Forward arguments to the TVDB Contribution Rail.",
        usage="animeta push [rail options]",
        add_help=False,
    )
    push.add_argument("push_args", nargs=argparse.REMAINDER)
    push.set_defaults(func=_push)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "reconstruct":
        return _reconstruct(argparse.Namespace(reconstruction_args=raw_args[1:]))
    if raw_args and raw_args[0] == "push":
        return _push(argparse.Namespace(push_args=raw_args[1:]))

    parser = build_parser()
    args = parser.parse_args(raw_args)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

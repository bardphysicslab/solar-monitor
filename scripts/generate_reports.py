#!/usr/bin/env python3
"""Generate disposable local diagnostics and optionally sync them with rclone."""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
GIT_REPORT = REPORTS_DIR / "git_diff.txt"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_app_config.py"
RCLONE_ENV = "BARDBOX_REPORTS_RCLONE_TARGET"


def run(command: List[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def git_text(*args: str) -> str:
    result = run(["git", *args], check=False)
    text = result.stdout
    if result.stderr:
        text += ("\n" if text and not text.endswith("\n") else "") + result.stderr
    return text.rstrip()


def generate_git_report() -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    branch = git_text("branch", "--show-current") or "(detached HEAD)"
    head = git_text("rev-parse", "HEAD")
    status = git_text("status", "--short") or "(clean)"
    unstaged = git_text("diff", "--no-ext-diff") or "(none)"
    staged = git_text("diff", "--cached", "--no-ext-diff") or "(none)"

    report = f"""BardBox Git Diagnostic Report
Generated UTC: {timestamp}
Repository: {REPO_ROOT}
Branch: {branch}
HEAD: {head}

=== git status --short ===
{status}

=== git diff ===
{unstaged}

=== git diff --cached ===
{staged}
"""
    GIT_REPORT.write_text(report, encoding="utf-8")
    return GIT_REPORT


def generate_config_report() -> int:
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--dry-run"],
        cwd=REPO_ROOT,
        text=True,
    )
    return result.returncode


def sync_reports(target: str) -> None:
    run(
        [
            "rclone",
            "copy",
            str(REPORTS_DIR),
            target,
            "--include",
            "*.txt",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="Skip config_sync_report.txt generation",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Skip git_diff.txt generation",
    )
    parser.add_argument(
        "--rclone-target",
        default=os.environ.get(RCLONE_ENV),
        help=f"Optional rclone destination folder; defaults to ${RCLONE_ENV}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    exit_code = 0

    if not args.no_config:
        exit_code = generate_config_report()

    if not args.no_git:
        path = generate_git_report()
        print(f"Git report: {path}")

    if args.rclone_target:
        sync_reports(args.rclone_target)
        print(f"Reports synced to: {args.rclone_target}")
    else:
        print(
            f"Reports not synced. Set {RCLONE_ENV} or pass --rclone-target to enable upload."
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

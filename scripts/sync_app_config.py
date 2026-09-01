#!/usr/bin/env python3
"""Safely synchronize ignored app_config.json with repository-owned defaults."""

import argparse
import json
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLE_PATH = REPO_ROOT / "raspi" / "config" / "app_config.example.json"
DEFAULT_LIVE_PATH = REPO_ROOT / "raspi" / "config" / "app_config.json"
DEFAULT_REPORT_PATH = REPO_ROOT / "reports" / "config_sync_report.txt"


class SyncSummary:
    def __init__(self) -> None:
        self.added_keys: List[str] = []
        self.added_node_fields: List[str] = []
        self.added_driver_fields: List[str] = []
        self.preserved_local_values: List[str] = []
        self.skipped_sections: List[str] = []
        self.backup_path: Optional[Path] = None

    @property
    def migration_required(self) -> bool:
        return bool(self.added_keys or self.added_node_fields or self.added_driver_fields)

    def render(self) -> str:
        sections = [
            render_list("Added keys", self.added_keys),
            render_list("Added node fields", self.added_node_fields),
            render_list("Added driver fields", self.added_driver_fields),
            render_list("Preserved local values", self.preserved_local_values),
            render_list("Skipped/unchanged sections", self.skipped_sections),
            f"Migration required: {'yes' if self.migration_required else 'no'}",
        ]
        return "\n".join(sections)

    def print(self) -> None:
        print(self.render())


def render_list(label: str, values: List[str]) -> str:
    if not values:
        return f"{label}: none"
    return "\n".join([f"{label}:", *(f"  - {value}" for value in values)])


def print_list(label: str, values: List[str]) -> None:
    """Backward-compatible output helper."""
    print(render_list(label, values))


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration root must be a JSON object: {path}")
    return value


def _summary_path(path: str, entry_kind: Optional[str], entry_uid: Optional[str]) -> str:
    if entry_kind is None or entry_uid is None:
        return path
    return f"{entry_kind}[uid={entry_uid}].{path}"


def _added_target(summary: SyncSummary, entry_kind: Optional[str]) -> List[str]:
    if entry_kind == "nodes":
        return summary.added_node_fields
    if entry_kind == "drivers":
        return summary.added_driver_fields
    return summary.added_keys


def merge_dicts(
    example: Dict[str, Any],
    local: Dict[str, Any],
    summary: SyncSummary,
    path: str = "",
    entry_kind: Optional[str] = None,
    entry_uid: Optional[str] = None,
) -> Dict[str, Any]:
    merged = deepcopy(local)
    for key, example_value in example.items():
        key_path = f"{path}.{key}" if path else key
        summary_path = _summary_path(key_path, entry_kind, entry_uid)
        if key not in local:
            merged[key] = deepcopy(example_value)
            _added_target(summary, entry_kind).append(summary_path)
        elif isinstance(example_value, dict) and isinstance(local[key], dict):
            merged[key] = merge_dicts(
                example_value, local[key], summary, key_path, entry_kind, entry_uid
            )
        elif (
            key in {"nodes", "drivers"}
            and entry_kind is None
            and isinstance(example_value, list)
            and isinstance(local[key], list)
        ):
            merged[key] = merge_uid_entries(key, example_value, local[key], summary)
        elif local[key] != example_value:
            summary.preserved_local_values.append(summary_path)
        else:
            summary.skipped_sections.append(f"{summary_path}: unchanged")
    for key in local:
        if key not in example:
            key_path = f"{path}.{key}" if path else key
            summary_path = _summary_path(key_path, entry_kind, entry_uid)
            summary.skipped_sections.append(f"{summary_path}: preserved local-only field")
    return merged


def merge_uid_entries(
    section: str,
    example_entries: List[Any],
    local_entries: List[Any],
    summary: SyncSummary,
) -> List[Any]:
    example_by_uid = {
        entry.get("uid"): entry
        for entry in example_entries
        if isinstance(entry, dict) and entry.get("uid") is not None
    }
    merged: List[Any] = []
    local_uids = set()
    singular = section[:-1] if section.endswith("s") else section
    for local_entry in local_entries:
        if not isinstance(local_entry, dict) or local_entry.get("uid") is None:
            merged.append(deepcopy(local_entry))
            summary.skipped_sections.append(f"{section}: preserved local {singular} without UID")
            continue
        uid_value = local_entry["uid"]
        uid = str(uid_value)
        local_uids.add(uid_value)
        example_entry = example_by_uid.get(uid_value)
        if example_entry is None:
            merged.append(deepcopy(local_entry))
            summary.skipped_sections.append(f"{section}[uid={uid}]: preserved local-only {singular}")
        else:
            merged.append(
                merge_dicts(
                    example_entry,
                    local_entry,
                    summary,
                    entry_kind=section,
                    entry_uid=uid,
                )
            )
    for uid in example_by_uid:
        if uid not in local_uids:
            summary.skipped_sections.append(f"{section}[uid={uid}]: skipped example-only {singular}")
    return merged


def merge_nodes(example_nodes: List[Any], local_nodes: List[Any], summary: SyncSummary) -> List[Any]:
    """Backward-compatible wrapper for callers/tests using the old helper."""
    return merge_uid_entries("nodes", example_nodes, local_nodes, summary)


def safe_write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def write_report(
    path: Path,
    summary: SyncSummary,
    example_path: Path,
    live_path: Path,
    write: bool,
    now: Optional[datetime] = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).astimezone().isoformat(timespec="seconds")
    mode = "write" if write else "check/dry-run"
    text = (
        f"Config sync report\n"
        f"Generated: {timestamp}\n"
        f"Mode: {mode}\n"
        f"Example: {example_path}\n"
        f"Live: {live_path}\n\n"
        f"{summary.render()}\n"
    )
    path.write_text(text, encoding="utf-8")


def synchronize(
    example_path: Path,
    live_path: Path,
    write: bool = False,
    now: Optional[datetime] = None,
) -> Tuple[SyncSummary, Dict[str, Any]]:
    example = load_json(example_path)
    summary = SyncSummary()
    if not live_path.exists():
        summary.added_keys.append("<live configuration file>")
        merged = deepcopy(example)
        if write:
            safe_write_json(live_path, merged)
        return summary, merged

    local = load_json(live_path)
    merged = merge_dicts(example, local, summary)
    if write:
        timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        summary.backup_path = live_path.with_name(f"{live_path.name}.backup-{timestamp}")
        shutil.copy2(live_path, summary.backup_path)
        safe_write_json(live_path, merged)
    return summary, merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", type=Path, default=DEFAULT_EXAMPLE_PATH)
    parser.add_argument("--live", type=Path, default=DEFAULT_LIVE_PATH)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="Report path (overwritten on every run)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Back up and atomically update the live config")
    mode.add_argument("--dry-run", action="store_true", help="Preview migration requirements without writing")
    mode.add_argument("--check", action="store_true", help="Exit non-zero when deployable fields are missing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary, _ = synchronize(args.example, args.live, write=args.write)
    summary.print()
    write_report(args.report, summary, args.example, args.live, args.write)
    if args.write:
        print(f"Updated: {args.live}")
        print(f"Backup: {summary.backup_path or 'none (live config was created)'}")
    else:
        print("DRY RUN: no files were modified.")
    print(f"Report: {args.report}")
    return 1 if args.check and summary.migration_required else 0


if __name__ == "__main__":
    raise SystemExit(main())

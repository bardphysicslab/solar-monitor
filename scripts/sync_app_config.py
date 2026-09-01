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


class SyncSummary:
    def __init__(self) -> None:
        self.added_keys: List[str] = []
        self.added_node_fields: List[str] = []
        self.preserved_local_values: List[str] = []
        self.skipped_sections: List[str] = []
        self.backup_path: Optional[Path] = None

    @property
    def migration_required(self) -> bool:
        return bool(self.added_keys or self.added_node_fields)

    def print(self) -> None:
        print_list("Added keys", self.added_keys)
        print_list("Added node fields", self.added_node_fields)
        print_list("Preserved local values", self.preserved_local_values)
        print_list("Skipped/unchanged sections", self.skipped_sections)
        print(f"Migration required: {'yes' if self.migration_required else 'no'}")


def print_list(label: str, values: List[str]) -> None:
    if not values:
        print(f"{label}: none")
        return
    print(f"{label}:")
    for value in values:
        print(f"  - {value}")


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration root must be a JSON object: {path}")
    return value


def merge_dicts(
    example: Dict[str, Any],
    local: Dict[str, Any],
    summary: SyncSummary,
    path: str = "",
    node_uid: Optional[str] = None,
) -> Dict[str, Any]:
    merged = deepcopy(local)
    for key, example_value in example.items():
        key_path = f"{path}.{key}" if path else key
        summary_path = f"nodes[uid={node_uid}].{key_path}" if node_uid is not None else key_path
        if key not in local:
            merged[key] = deepcopy(example_value)
            target = summary.added_node_fields if node_uid is not None else summary.added_keys
            target.append(summary_path)
        elif isinstance(example_value, dict) and isinstance(local[key], dict):
            merged[key] = merge_dicts(example_value, local[key], summary, key_path, node_uid)
        elif key == "nodes" and node_uid is None and isinstance(example_value, list) and isinstance(local[key], list):
            merged[key] = merge_nodes(example_value, local[key], summary)
        elif local[key] != example_value:
            summary.preserved_local_values.append(summary_path)
        else:
            summary.skipped_sections.append(f"{summary_path}: unchanged")
    for key in local:
        if key not in example:
            key_path = f"{path}.{key}" if path else key
            summary_path = f"nodes[uid={node_uid}].{key_path}" if node_uid is not None else key_path
            summary.skipped_sections.append(f"{summary_path}: preserved local-only field")
    return merged


def merge_nodes(example_nodes: List[Any], local_nodes: List[Any], summary: SyncSummary) -> List[Any]:
    example_by_uid = {
        node.get("uid"): node
        for node in example_nodes
        if isinstance(node, dict) and node.get("uid") is not None
    }
    merged: List[Any] = []
    local_uids = set()
    for local_node in local_nodes:
        if not isinstance(local_node, dict) or local_node.get("uid") is None:
            merged.append(deepcopy(local_node))
            summary.skipped_sections.append("nodes: preserved local node without UID")
            continue
        uid = str(local_node["uid"])
        local_uids.add(local_node["uid"])
        example_node = example_by_uid.get(local_node["uid"])
        if example_node is None:
            merged.append(deepcopy(local_node))
            summary.skipped_sections.append(f"nodes[uid={uid}]: preserved local-only node")
        else:
            merged.append(merge_dicts(example_node, local_node, summary, node_uid=uid))
    for uid in example_by_uid:
        if uid not in local_uids:
            summary.skipped_sections.append(f"nodes[uid={uid}]: skipped example-only node")
    return merged


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Back up and atomically update the live config")
    mode.add_argument("--dry-run", action="store_true", help="Preview migration requirements without writing")
    mode.add_argument("--check", action="store_true", help="Exit non-zero when deployable fields are missing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary, _ = synchronize(args.example, args.live, write=args.write)
    summary.print()
    if args.write:
        print(f"Updated: {args.live}")
        print(f"Backup: {summary.backup_path or 'none (live config was created)'}")
    else:
        print("DRY RUN: no files were modified.")
    return 1 if args.check and summary.migration_required else 0


if __name__ == "__main__":
    raise SystemExit(main())

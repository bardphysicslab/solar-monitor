#!/usr/bin/env python3

import argparse
import copy
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def merge_missing(live: Any, example: Any) -> Any:
    """Add missing example fields without overwriting live values."""
    if isinstance(live, dict) and isinstance(example, dict):
        merged = copy.deepcopy(live)

        for key, example_value in example.items():
            if key not in merged:
                merged[key] = copy.deepcopy(example_value)
            else:
                merged[key] = merge_missing(merged[key], example_value)

        return merged

    return copy.deepcopy(live)


def merge_nodes(
    live_nodes: list[dict[str, Any]],
    example_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Add missing fields to matching nodes by UID.

    Existing production nodes are preserved.
    Example-only nodes are not automatically added.
    """
    example_by_uid = {
        node["uid"]: node
        for node in example_nodes
        if isinstance(node, dict) and node.get("uid")
    }

    merged_nodes: list[dict[str, Any]] = []

    for live_node in live_nodes:
        if not isinstance(live_node, dict):
            merged_nodes.append(copy.deepcopy(live_node))
            continue

        uid = live_node.get("uid")
        matching_example = example_by_uid.get(uid)

        if matching_example is None:
            merged_nodes.append(copy.deepcopy(live_node))
        else:
            merged_nodes.append(merge_missing(live_node, matching_example))

    return merged_nodes


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add missing fields from app_config.example.json to app_config.json "
            "without overwriting deployment-specific values."
        )
    )
    parser.add_argument(
        "--example",
        type=Path,
        default=Path("raspi/config/app_config.example.json"),
    )
    parser.add_argument(
        "--live",
        type=Path,
        default=Path("raspi/config/app_config.json"),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Back up and update the live file. Default behavior is preview only.",
    )
    args = parser.parse_args()

    if not args.example.is_file():
        raise SystemExit(f"Example config not found: {args.example}")

    if not args.live.is_file():
        raise SystemExit(f"Live config not found: {args.live}")

    with args.example.open("r", encoding="utf-8") as handle:
        example = json.load(handle)

    with args.live.open("r", encoding="utf-8") as handle:
        live = json.load(handle)

    merged = merge_missing(live, example)

    live_nodes = live.get("nodes", [])
    example_nodes = example.get("nodes", [])

    if isinstance(live_nodes, list) and isinstance(example_nodes, list):
        merged["nodes"] = merge_nodes(live_nodes, example_nodes)

    rendered = json.dumps(merged, indent=2) + "\n"

    if not args.write:
        print(rendered)
        print("\nPreview only. No files were changed.")
        print("Run again with --write to update the live configuration.")
        return

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = args.live.with_name(
        f"{args.live.name}.backup-{timestamp}"
    )
    temporary_path = args.live.with_suffix(args.live.suffix + ".tmp")

    shutil.copy2(args.live, backup_path)
    temporary_path.write_text(rendered, encoding="utf-8")
    temporary_path.replace(args.live)

    print(f"Updated: {args.live}")
    print(f"Backup:  {backup_path}")


if __name__ == "__main__":
    main()

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import sync_app_config as sync


def test_default_paths_use_solar_monitor_layout():
    assert sync.DEFAULT_EXAMPLE_PATH == (
        REPO_ROOT / "raspi" / "config" / "app_config.example.json"
    )
    assert sync.DEFAULT_LIVE_PATH == REPO_ROOT / "raspi" / "config" / "app_config.json"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def configs(tmp_path, example, live):
    example_path = tmp_path / "app_config.example.json"
    live_path = tmp_path / "app_config.json"
    write_json(example_path, example)
    write_json(live_path, live)
    return example_path, live_path


def test_detects_missing_keys_and_preserves_local_values(tmp_path):
    example_path, live_path = configs(
        tmp_path,
        {"new_top": 1, "service": {"new_nested": True, "port": 8000}},
        {"service": {"port": 9000}, "local_only": "keep"},
    )
    summary, merged = sync.synchronize(example_path, live_path)
    assert summary.added_keys == ["new_top", "service.new_nested"]
    assert "service.port" in summary.preserved_local_values
    assert merged == {
        "new_top": 1,
        "service": {"new_nested": True, "port": 9000},
        "local_only": "keep",
    }


def test_nodes_merge_by_uid_without_adding_or_removing_nodes(tmp_path):
    example_path, live_path = configs(
        tmp_path,
        {"nodes": [
            {"uid": "bb-test-001", "location": "example", "new_field": 7},
            {"uid": "bb-example-only", "new_field": 8},
        ]},
        {"nodes": [
            {"uid": "bb-test-001", "location": "production"},
            {"uid": "bb-local-only", "secret": "preserved"},
        ]},
    )
    summary, merged = sync.synchronize(example_path, live_path)
    assert summary.added_node_fields == ["nodes[uid=bb-test-001].new_field"]
    assert "nodes[uid=bb-test-001].location" in summary.preserved_local_values
    assert [node["uid"] for node in merged["nodes"]] == ["bb-test-001", "bb-local-only"]
    assert merged["nodes"][1]["secret"] == "preserved"
    assert any("skipped example-only node" in item for item in summary.skipped_sections)


def test_drivers_merge_by_uid_without_adding_or_removing_drivers(tmp_path):
    example_path, live_path = configs(
        tmp_path,
        {"drivers": [
            {
                "driver": "et54",
                "uid": "load-001",
                "config": {"port": "/dev/example", "limits": {"max_power_w": 200}},
            },
            {"driver": "spn1", "uid": "example-only", "config": {"baud": 9600}},
        ]},
        {"drivers": [
            {
                "driver": "et54",
                "uid": "load-001",
                "config": {"port": "/dev/local"},
            },
            {"driver": "custom", "uid": "local-only", "config": {"secret": "preserved"}},
        ]},
    )
    summary, merged = sync.synchronize(example_path, live_path)
    assert summary.added_driver_fields == [
        "drivers[uid=load-001].config.limits"
    ]
    assert "drivers[uid=load-001].config.port" in summary.preserved_local_values
    assert [driver["uid"] for driver in merged["drivers"]] == ["load-001", "local-only"]
    assert merged["drivers"][0]["config"]["port"] == "/dev/local"
    assert merged["drivers"][0]["config"]["limits"] == {"max_power_w": 200}
    assert merged["drivers"][1]["config"]["secret"] == "preserved"
    assert any("drivers[uid=example-only]: skipped example-only driver" == item for item in summary.skipped_sections)


def test_driver_fields_require_migration(tmp_path):
    example_path, live_path = configs(
        tmp_path,
        {"drivers": [{"uid": "load-001", "config": {"new_safety_field": True}}]},
        {"drivers": [{"uid": "load-001", "config": {}}]},
    )
    summary, _ = sync.synchronize(example_path, live_path)
    assert summary.migration_required
    assert summary.added_driver_fields == [
        "drivers[uid=load-001].config.new_safety_field"
    ]


def test_dry_run_does_not_write(tmp_path):
    example_path, live_path = configs(tmp_path, {"new": True}, {"existing": True})
    original = live_path.read_bytes()
    summary, _ = sync.synchronize(example_path, live_path, write=False)
    assert summary.migration_required
    assert live_path.read_bytes() == original
    assert not list(tmp_path.glob("*.backup-*"))


def test_write_creates_timestamped_backup_and_updates_atomically(tmp_path):
    example_path, live_path = configs(tmp_path, {"new": True}, {"existing": True})
    original = live_path.read_bytes()
    with mock.patch.object(sync.os, "replace", wraps=os.replace) as replace:
        summary, _ = sync.synchronize(
            example_path, live_path, write=True,
            now=datetime(2026, 8, 21, 12, 34, 56),
        )
    assert summary.backup_path == tmp_path / "app_config.json.backup-20260821-123456"
    assert summary.backup_path.read_bytes() == original
    assert json.loads(live_path.read_text(encoding="utf-8")) == {"existing": True, "new": True}
    replace.assert_called_once()
    temporary, destination = replace.call_args.args
    assert Path(destination) == live_path
    assert Path(temporary).parent == live_path.parent
    assert not list(tmp_path.glob(".app_config.json.*.tmp"))


@pytest.mark.parametrize(
    ("live", "expected_code", "expected_line"),
    [({"required": True}, 0, "Added keys: none"), ({}, 1, "Added keys:")],
)
def test_check_exit_status(tmp_path, live, expected_code, expected_line):
    example_path, live_path = configs(tmp_path, {"required": True}, live)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sync_app_config.py"),
         "--check", "--example", str(example_path), "--live", str(live_path)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == expected_code
    assert expected_line in result.stdout
    assert "Added node fields: none" in result.stdout
    assert "Added driver fields: none" in result.stdout
    assert "DRY RUN: no files were modified." in result.stdout

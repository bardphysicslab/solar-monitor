import tempfile
import unittest
from pathlib import Path

from raspi.backup import (
    DataBackupManager,
    BackupConfig,
    backup_config_from_app_config,
    parse_backup_interval_minutes,
    render_systemd_timer,
)


class FakeRecorder:
    def __init__(self):
        import threading

        self.lock = threading.RLock()
        self.flush_due_calls = 0

    def flush_due(self):
        self.flush_due_calls += 1


class BackupTest(unittest.TestCase):
    def test_backup_config_defaults_and_invalid_interval(self):
        config = backup_config_from_app_config({})

        self.assertFalse(config.enabled)
        self.assertEqual(config.interval_minutes, 30)
        self.assertEqual(parse_backup_interval_minutes(0), 30)
        self.assertEqual(parse_backup_interval_minutes("bad"), 30)

    def test_backup_config_reads_interval_and_destination(self):
        config = backup_config_from_app_config(
            {
                "backup": {
                    "enabled": True,
                    "interval_minutes": 5,
                    "remote": "bardbox",
                    "destination": "solar-monitor/test",
                }
            }
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.interval_minutes, 5)
        self.assertEqual(config.remote, "bardbox")
        self.assertEqual(config.destination, "solar-monitor/test")

    def test_snapshot_captures_complete_csv_without_corrupting_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "sensor_data"
            csv_dir = data_root / "spn1-0001"
            csv_dir.mkdir(parents=True)
            csv_file = csv_dir / "2026-07-19.csv"
            csv_file.write_text("header\nrow1\nrow2\n", encoding="utf-8")
            recorder = FakeRecorder()
            manager = DataBackupManager(
                BackupConfig(enabled=True, remote="bardbox", destination="solar-monitor"),
                data_root=data_root,
                snapshot_root=root / "snapshots",
                recorder=recorder,
            )

            snapshot = manager.create_snapshot()
            csv_file.write_text("header\nrow1\nrow2\nrow3\n", encoding="utf-8")

            snapshot_file = snapshot / "sensor_data" / "spn1-0001" / "2026-07-19.csv"
            self.assertEqual(snapshot_file.read_text(encoding="utf-8"), "header\nrow1\nrow2\n")
            self.assertEqual(csv_file.read_text(encoding="utf-8"), "header\nrow1\nrow2\nrow3\n")
            self.assertEqual(recorder.flush_due_calls, 1)

    def test_backup_sync_uses_snapshot_directory_not_latest_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "sensor_data"
            csv_dir = data_root / "spn1-0001"
            csv_dir.mkdir(parents=True)
            (csv_dir / "2026-07-19.csv").write_text("header\nrow1\nrow2\n", encoding="utf-8")
            calls = []

            def fake_run(command, check):
                calls.append((command, check))

            manager = DataBackupManager(
                BackupConfig(enabled=True, remote="bardbox", destination="solar-monitor"),
                data_root=data_root,
                snapshot_root=root / "snapshots",
                recorder=FakeRecorder(),
                run_command=fake_run,
            )

            result = manager.backup_once("2026-07-19T12:00:00Z")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(calls[0][0][0:2], ["rclone", "sync"])
            self.assertIn("solar-monitor-", calls[0][0][2])
            self.assertEqual(calls[0][0][3], "bardbox:solar-monitor")

    def test_timer_reflects_configured_interval(self):
        timer = render_systemd_timer(30)

        self.assertIn("OnUnitActiveSec=1800s", timer)
        self.assertIn("OnBootSec=1800s", timer)


if __name__ == "__main__":
    unittest.main()

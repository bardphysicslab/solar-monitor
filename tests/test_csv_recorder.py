import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from raspi.recording.csv_recorder import (
    CsvAveragingRecorder,
    CsvRecorderError,
    RecorderConfig,
    parse_interval_s,
    recorder_configs_from_app_config,
)


class FakeClock:
    def __init__(self):
        self.monotonic = 0.0
        self.utcnow = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.monotonic += seconds
        self.utcnow += timedelta(seconds=seconds)


def spn1_reading(total=None, diffuse=None, sun=None, status="ok"):
    data = {}
    if total is not None:
        data["total_w_m2"] = total
    if diffuse is not None:
        data["diffuse_w_m2"] = diffuse
    if sun is not None:
        data["sun"] = sun
    return {"uid": "spn1-0001", "timestamp": "ignored", "status": status, "data": data, "extended": {}, "raw": None}


def solar_reading(data=None, extended=None, status="ok"):
    return {
        "uid": "bb-solar-pnl-001",
        "timestamp": "ignored",
        "status": status,
        "data": data or {},
        "extended": extended or {},
        "raw": None,
    }


class FailingOnceRecorder(CsvAveragingRecorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_next_uid = None

    def _write_row(self, uid, driver_name, end_utc, row):
        if self.fail_next_uid == uid:
            self.fail_next_uid = None
            raise OSError("disk full")
        return super()._write_row(uid, driver_name, end_utc, row)


class CsvRecorderTest(unittest.TestCase):
    def make_recorder(self, configs, root):
        self.clock = FakeClock()
        recorder = CsvAveragingRecorder(
            configs,
            data_root=Path(root),
            monotonic_fn=lambda: self.clock.monotonic,
            utcnow_fn=lambda: self.clock.utcnow,
            fsync=False,
        )
        recorder.start()
        return recorder

    def rows_for(self, root, uid, date="2026-07-16"):
        path = Path(root) / uid / f"{date}.csv"
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_recording_config_defaults_and_intervals(self):
        configs = recorder_configs_from_app_config(
            {
                "drivers": [
                    {"driver": "spn1", "uid": "spn1-0001", "config": {}},
                    {
                        "driver": "wifi_node",
                        "uid": "bb-solar-pnl-001",
                        "config": {"recording": {"enabled": True, "interval_s": 3, "mode": "mean"}},
                    },
                    {
                        "driver": "wifi_node",
                        "uid": "bb-solar-pnl-002",
                        "config": {"recording": {"enabled": True, "interval_s": 5}},
                    },
                ]
            }
        )

        by_uid = {config.uid: config for config in configs}
        self.assertFalse(by_uid["spn1-0001"].enabled)
        self.assertEqual(by_uid["spn1-0001"].interval_s, 10)
        self.assertEqual(by_uid["bb-solar-pnl-001"].interval_s, 3)
        self.assertEqual(by_uid["bb-solar-pnl-002"].interval_s, 5)
        self.assertEqual(parse_interval_s(0, "bad"), 10)

    def test_unsupported_mode_is_rejected(self):
        with self.assertRaisesRegex(CsvRecorderError, "Unsupported recording mode"):
            recorder_configs_from_app_config(
                {
                    "drivers": [
                        {
                            "driver": "spn1",
                            "uid": "spn1-0001",
                            "config": {"recording": {"enabled": True, "mode": "median"}},
                        }
                    ]
                }
            )

    def test_spn1_window_averages_and_sun_majority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=10)],
                temp_dir,
            )

            for total, diffuse, sun in ((100, 30, 0), (102, 31, 0), (98, 29, 1), (100, 30, 0)):
                recorder.add_reading("spn1-0001", spn1_reading(total, diffuse, sun))
                self.clock.advance(2)

            self.clock.advance(2)
            recorder.flush_due()

            row = self.rows_for(temp_dir, "spn1-0001")[0]
            self.assertEqual(row["total_w_m2"], "100.000")
            self.assertEqual(row["diffuse_w_m2"], "30.000")
            self.assertEqual(row["sun"], "0")
            self.assertEqual(row["sample_count"], "4")
            self.assertEqual(row["timestamp_utc"], row["window_end_utc"])
            self.assertEqual(row["window_start_utc"], "2026-07-16T12:00:00Z")

    def test_sample_after_interval_starts_next_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=10)],
                temp_dir,
            )

            recorder.add_reading("spn1-0001", spn1_reading(100, 30, 1))
            self.clock.advance(11)
            recorder.add_reading("spn1-0001", spn1_reading(200, 60, 0))
            recorder.flush_all()

            rows = self.rows_for(temp_dir, "spn1-0001")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["total_w_m2"], "100.000")
            self.assertEqual(rows[0]["sample_count"], "1")
            self.assertEqual(rows[1]["total_w_m2"], "200.000")
            self.assertEqual(rows[1]["sample_count"], "1")

    def test_thirty_second_run_with_ten_second_windows_writes_three_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=10)],
                temp_dir,
            )

            for second in range(30):
                recorder.add_reading("spn1-0001", spn1_reading(100 + second, 30, second % 2))
                self.clock.advance(1)
                recorder.flush_due()

            rows = self.rows_for(temp_dir, "spn1-0001")
            self.assertEqual(len(rows), 3)
            self.assertEqual([row["sample_count"] for row in rows], ["10", "10", "10"])
            self.assertEqual(rows[0]["total_w_m2"], "104.500")
            self.assertEqual(rows[1]["total_w_m2"], "114.500")
            self.assertEqual(rows[2]["total_w_m2"], "124.500")

    def test_binary_tie_uses_latest_valid_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=1)],
                temp_dir,
            )

            for sun in (0, 1, 0, 1):
                recorder.add_reading("spn1-0001", spn1_reading(1, 1, sun))

            self.clock.advance(1)
            recorder.flush_due()

            row = self.rows_for(temp_dir, "spn1-0001")[0]
            self.assertEqual(row["sun"], "1")

    def test_solar_window_maps_single_channel_and_blanks_missing_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("bb-solar-pnl-001", "wifi_node", enabled=True, interval_s=10)],
                temp_dir,
            )

            for voltage, ok, rssi in ((5.0, 1, -40), (6.0, 1, -42), (7.0, 0, -41)):
                recorder.add_reading(
                    "bb-solar-pnl-001",
                    solar_reading(
                        {"panel_voltage_v": voltage},
                        {"voltage_ok": ok, "rssi_dbm": rssi},
                    ),
                )
                self.clock.advance(2)

            self.clock.advance(4)
            recorder.flush_due()

            row = self.rows_for(temp_dir, "bb-solar-pnl-001")[0]
            self.assertEqual(row["panel_voltage_1_v"], "6.0000")
            self.assertEqual(row["panel_voltage_2_v"], "")
            self.assertEqual(row["panel_voltage_3_v"], "")
            self.assertEqual(row["panel_voltage_4_v"], "")
            self.assertEqual(row["voltage_ok"], "1")
            self.assertEqual(row["rssi_dbm"], "-41.0")
            self.assertEqual(row["sample_count"], "3")

    def test_solar_four_channel_values_are_averaged_independently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("bb-solar-pnl-001", "wifi_node", enabled=True, interval_s=1)],
                temp_dir,
            )

            recorder.add_reading(
                "bb-solar-pnl-001",
                solar_reading(
                    {
                        "panel_voltage_1_v": 1,
                        "panel_voltage_2_v": 2,
                        "panel_voltage_3_v": None,
                        "panel_voltage_4_v": 4,
                    },
                    {"voltage_ok": 0, "rssi_dbm": -40},
                ),
            )
            recorder.add_reading(
                "bb-solar-pnl-001",
                solar_reading(
                    {
                        "panel_voltage_1_v": 3,
                        "panel_voltage_2_v": float("nan"),
                        "panel_voltage_3_v": 9,
                        "panel_voltage_4_v": float("inf"),
                    },
                    {"voltage_ok": 1, "rssi_dbm": -42},
                ),
            )

            self.clock.advance(1)
            recorder.flush_due()

            row = self.rows_for(temp_dir, "bb-solar-pnl-001")[0]
            self.assertEqual(row["panel_voltage_1_v"], "2.0000")
            self.assertEqual(row["panel_voltage_2_v"], "2.0000")
            self.assertEqual(row["panel_voltage_3_v"], "9.0000")
            self.assertEqual(row["panel_voltage_4_v"], "4.0000")
            self.assertEqual(row["voltage_ok"], "1")

    def test_error_readings_are_excluded_and_stopped_recorder_does_not_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=1)],
                temp_dir,
            )

            recorder.add_reading("spn1-0001", spn1_reading(1, 1, 1, status="error"))
            self.clock.advance(1)
            recorder.flush_due()
            self.assertFalse((Path(temp_dir) / "spn1-0001").exists())

            recorder.stop()
            recorder.add_reading("spn1-0001", spn1_reading(2, 2, 1))
            recorder.flush_all()
            self.assertFalse((Path(temp_dir) / "spn1-0001").exists())

    def test_header_once_append_and_utc_date_rotation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=1)],
                temp_dir,
            )

            recorder.add_reading("spn1-0001", spn1_reading(1, 1, 1))
            self.clock.advance(1)
            recorder.flush_due()

            recorder.add_reading("spn1-0001", spn1_reading(2, 2, 0))
            self.clock.advance(1)
            recorder.flush_due()

            current_file = Path(temp_dir) / "spn1-0001" / "2026-07-16.csv"
            lines = current_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(lines[0].count("timestamp_utc"), 1)

            self.clock.utcnow = datetime(2026, 7, 17, 0, 0, 0, tzinfo=timezone.utc)
            recorder.add_reading("spn1-0001", spn1_reading(3, 3, 1))
            self.clock.advance(1)
            recorder.flush_due()
            self.assertTrue((Path(temp_dir) / "spn1-0001" / "2026-07-17.csv").exists())

    def test_stop_flushes_partial_window_and_status_reports_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=100)],
                temp_dir,
            )

            recorder.add_reading("spn1-0001", spn1_reading(4, 2, 1))
            status_before = recorder.status()["drivers"]["spn1-0001"]
            self.assertEqual(status_before["current_sample_count"], 1)
            self.assertEqual(status_before["current_window_start_utc"], "2026-07-16T12:00:00Z")

            self.clock.advance(5)
            recorder.stop()

            row = self.rows_for(temp_dir, "spn1-0001")[0]
            self.assertEqual(row["sample_count"], "1")
            status_after = recorder.status()["drivers"]["spn1-0001"]
            self.assertIsNone(status_after["current_window_start_utc"])
            self.assertIsNotNone(status_after["last_write_utc"])
            self.assertIsNotNone(status_after["last_file"])

    def test_flush_uid_flushes_only_that_device(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = self.make_recorder(
                [
                    RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=100),
                    RecorderConfig("bb-solar-pnl-001", "wifi_node", enabled=True, interval_s=100),
                ],
                temp_dir,
            )

            recorder.add_reading("spn1-0001", spn1_reading(4, 2, 1))
            recorder.add_reading(
                "bb-solar-pnl-001",
                solar_reading({"panel_voltage_v": 5}, {"voltage_ok": 1, "rssi_dbm": -40}),
            )

            recorder.flush_uid("spn1-0001")

            self.assertTrue((Path(temp_dir) / "spn1-0001" / "2026-07-16.csv").exists())
            self.assertFalse((Path(temp_dir) / "bb-solar-pnl-001").exists())
            self.assertEqual(recorder.status()["drivers"]["bb-solar-pnl-001"]["current_sample_count"], 1)

    def test_failed_write_retains_window_and_other_uid_can_continue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.clock = FakeClock()
            recorder = FailingOnceRecorder(
                [
                    RecorderConfig("spn1-0001", "spn1", enabled=True, interval_s=1),
                    RecorderConfig("bb-solar-pnl-001", "wifi_node", enabled=True, interval_s=1),
                ],
                data_root=Path(temp_dir),
                monotonic_fn=lambda: self.clock.monotonic,
                utcnow_fn=lambda: self.clock.utcnow,
                fsync=False,
            )
            recorder.start()
            recorder.fail_next_uid = "spn1-0001"

            recorder.add_reading("spn1-0001", spn1_reading(1, 1, 1))
            recorder.add_reading(
                "bb-solar-pnl-001",
                solar_reading({"panel_voltage_v": 5}, {"voltage_ok": 1, "rssi_dbm": -40}),
            )
            self.clock.advance(1)
            recorder.flush_due()

            self.assertIn("disk full", recorder.status()["drivers"]["spn1-0001"]["last_error"])
            self.assertTrue((Path(temp_dir) / "bb-solar-pnl-001" / "2026-07-16.csv").exists())

            recorder.flush_due()
            self.assertTrue((Path(temp_dir) / "spn1-0001" / "2026-07-16.csv").exists())
            self.assertIsNone(recorder.status()["drivers"]["spn1-0001"]["last_error"])


if __name__ == "__main__":
    unittest.main()

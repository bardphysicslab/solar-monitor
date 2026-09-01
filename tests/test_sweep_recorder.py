import csv
import json
import tempfile
import unittest
from pathlib import Path

from raspi.recording.sweep_recorder import SweepRecorder


class SweepRecorderTest(unittest.TestCase):
    def test_raw_points_and_one_derived_summary_are_persisted(self):
        result = {
            "sweep_id": "sweep-001",
            "started_at": "2026-09-01T12:00:00Z",
            "completed_at": "2026-09-01T12:00:05Z",
            "panel_uid": "panel-001",
            "load_uid": "load-001",
            "load_type": "electronic_load",
            "active_mode": "sweep",
            "quality": "valid",
            "reason": None,
            "points": [
                {
                    "timestamp": "2026-09-01T12:00:01Z",
                    "resistance_setpoint_ohm": 1000,
                    "voltage_v": 18,
                    "current_a": 0.03,
                    "power_w": 0.54,
                    "load_resistance_ohm": 1000,
                }
            ],
            "vmpp_v": 18,
            "impp_a": 0.03,
            "pmpp_w": 0.54,
            "rmpp_ohm": 1000,
            "irradiance": {
                "count": 3,
                "mean_w_m2": 500,
                "min_w_m2": 498,
                "max_w_m2": 502,
                "std_w_m2": 1.63,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = SweepRecorder(Path(temp_dir), fsync=False)
            recorder.record(result)
            directory = Path(temp_dir) / "load-001" / "sweeps"
            raw = json.loads((directory / "sweep-001.json").read_text(encoding="utf-8"))
            with (directory / "2026-09-01-summary.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(raw["points"], result["points"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sweep_id"], "sweep-001")
        self.assertEqual(rows[0]["point_count"], "1")
        self.assertEqual(rows[0]["panel_uid"], "panel-001")
        self.assertEqual(rows[0]["pmpp_w"], "0.54")


if __name__ == "__main__":
    unittest.main()

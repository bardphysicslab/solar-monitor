import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


SUMMARY_FIELDS = [
    "sweep_id",
    "started_at",
    "completed_at",
    "panel_uid",
    "load_uid",
    "load_type",
    "active_mode",
    "quality",
    "reason",
    "point_count",
    "vmpp_v",
    "impp_a",
    "pmpp_w",
    "rmpp_ohm",
    "irradiance_mean_w_m2",
    "irradiance_min_w_m2",
    "irradiance_max_w_m2",
    "irradiance_std_w_m2",
]


class SweepRecorder:
    def __init__(self, data_root: Path, fsync: bool = True):
        self.data_root = Path(data_root)
        self.fsync = fsync

    def record(self, result: Dict[str, Any]) -> None:
        load_uid = str(result["load_uid"])
        completed = datetime.fromisoformat(result["completed_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
        directory = self.data_root / load_uid / "sweeps"
        directory.mkdir(parents=True, exist_ok=True)
        raw_path = directory / f"{result['sweep_id']}.json"
        temporary = directory / f".{result['sweep_id']}.json.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        temporary.replace(raw_path)

        summary_path = directory / f"{completed.date().isoformat()}-summary.csv"
        needs_header = not summary_path.exists() or summary_path.stat().st_size == 0
        irradiance = result.get("irradiance") or {}
        row = {
            "sweep_id": result.get("sweep_id"),
            "started_at": result.get("started_at"),
            "completed_at": result.get("completed_at"),
            "panel_uid": result.get("panel_uid"),
            "load_uid": result.get("load_uid"),
            "load_type": result.get("load_type"),
            "active_mode": result.get("active_mode"),
            "quality": result.get("quality"),
            "reason": result.get("reason"),
            "point_count": len(result.get("points") or []),
            "vmpp_v": result.get("vmpp_v"),
            "impp_a": result.get("impp_a"),
            "pmpp_w": result.get("pmpp_w"),
            "rmpp_ohm": result.get("rmpp_ohm"),
            "irradiance_mean_w_m2": irradiance.get("mean_w_m2"),
            "irradiance_min_w_m2": irradiance.get("min_w_m2"),
            "irradiance_max_w_m2": irradiance.get("max_w_m2"),
            "irradiance_std_w_m2": irradiance.get("std_w_m2"),
        }
        with summary_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

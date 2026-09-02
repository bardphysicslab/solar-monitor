import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


SUMMARY_FIELDS = [
    "sweep_id",
    "started_at",
    "scheduled_start_at",
    "actual_started_at",
    "completed_at",
    "duration_s",
    "timing_status",
    "panel_uid",
    "load_uid",
    "load_type",
    "active_mode",
    "quality",
    "electrical_status",
    "irradiance_status",
    "reason",
    "point_count",
    "sweep_center_resistance_ohm",
    "center_source",
    "estimated_rmpp_ohm",
    "mpp_bracketed",
    "recovery_used",
    "irradiance_used_w_m2",
    "module_temperature_c",
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
            "scheduled_start_at": result.get("scheduled_start_at"),
            "actual_started_at": result.get("actual_started_at"),
            "completed_at": result.get("completed_at"),
            "duration_s": result.get("duration_s"),
            "timing_status": result.get("timing_status"),
            "panel_uid": result.get("panel_uid"),
            "load_uid": result.get("load_uid"),
            "load_type": result.get("load_type"),
            "active_mode": result.get("active_mode"),
            "quality": result.get("quality"),
            "electrical_status": result.get("electrical_status"),
            "irradiance_status": result.get("irradiance_status"),
            "reason": result.get("reason"),
            "point_count": len(result.get("points") or []),
            "sweep_center_resistance_ohm": result.get("sweep_center_resistance_ohm"),
            "center_source": result.get("center_source"),
            "estimated_rmpp_ohm": result.get("estimated_rmpp_ohm"),
            "mpp_bracketed": result.get("mpp_bracketed"),
            "recovery_used": result.get("recovery_used"),
            "irradiance_used_w_m2": result.get("irradiance_used_w_m2"),
            "module_temperature_c": result.get("module_temperature_c"),
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

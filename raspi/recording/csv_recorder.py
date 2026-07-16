import csv
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 10.0
SUPPORTED_MODE = "mean"

SPN1_HEADER = [
    "timestamp_utc",
    "window_start_utc",
    "window_end_utc",
    "sample_count",
    "total_w_m2",
    "diffuse_w_m2",
    "sun",
]

SOLAR_HEADER = [
    "timestamp_utc",
    "window_start_utc",
    "window_end_utc",
    "sample_count",
    "panel_voltage_1_v",
    "panel_voltage_2_v",
    "panel_voltage_3_v",
    "panel_voltage_4_v",
    "voltage_ok",
    "rssi_dbm",
]


class CsvRecorderError(Exception):
    """Raised when recorder configuration or writes fail."""


@dataclass
class RecorderConfig:
    uid: str
    driver_name: str
    enabled: bool = False
    interval_s: float = DEFAULT_INTERVAL_S
    mode: str = SUPPORTED_MODE


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_recording_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_interval_s(value: Any, uid: str) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid recording interval_s for %s; falling back to %.0f seconds", uid, DEFAULT_INTERVAL_S)
        return DEFAULT_INTERVAL_S

    if interval <= 0:
        logger.warning("Invalid recording interval_s for %s; falling back to %.0f seconds", uid, DEFAULT_INTERVAL_S)
        return DEFAULT_INTERVAL_S

    return interval


def parse_recording_mode(value: Any, uid: str) -> str:
    mode = str(value or SUPPORTED_MODE).strip().lower()
    if mode != SUPPORTED_MODE:
        raise CsvRecorderError(f"Unsupported recording mode for {uid}: {mode}")
    return mode


def recorder_configs_from_app_config(config: Dict[str, Any]) -> List[RecorderConfig]:
    configs = []
    for entry in config.get("drivers", []):
        uid = entry.get("uid")
        driver_name = entry.get("driver")
        if not uid or not driver_name:
            continue

        driver_config = entry.get("config", {})
        recording_config = driver_config.get("recording", {})
        configs.append(
            RecorderConfig(
                uid=uid,
                driver_name=driver_name,
                enabled=parse_recording_enabled(recording_config.get("enabled", False)),
                interval_s=parse_interval_s(recording_config.get("interval_s", DEFAULT_INTERVAL_S), uid),
                mode=parse_recording_mode(recording_config.get("mode", SUPPORTED_MODE), uid),
            )
        )
    return configs


def is_valid_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def is_valid_binary(value: Any) -> bool:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return number in (0, 1)


class AveragingWindow:
    def __init__(self, start_monotonic: float, start_utc: datetime, driver_name: str):
        self.start_monotonic = start_monotonic
        self.start_utc = start_utc
        self.driver_name = driver_name
        self.sample_count = 0
        self.numeric_sums: Dict[str, float] = {}
        self.numeric_counts: Dict[str, int] = {}
        self.binary_ones: Dict[str, int] = {}
        self.binary_zeros: Dict[str, int] = {}
        self.binary_latest: Dict[str, int] = {}
        self.completed_end_utc: Optional[datetime] = None
        self.pending_row: Optional[Dict[str, str]] = None

    @property
    def pending_write(self) -> bool:
        return self.pending_row is not None

    def add_numeric(self, field: str, value: Any) -> None:
        if not is_valid_number(value):
            return
        number = float(value)
        self.numeric_sums[field] = self.numeric_sums.get(field, 0.0) + number
        self.numeric_counts[field] = self.numeric_counts.get(field, 0) + 1

    def add_binary(self, field: str, value: Any) -> None:
        if not is_valid_binary(value):
            return
        number = int(value)
        if number == 1:
            self.binary_ones[field] = self.binary_ones.get(field, 0) + 1
        else:
            self.binary_zeros[field] = self.binary_zeros.get(field, 0) + 1
        self.binary_latest[field] = number

    def mean(self, field: str) -> Optional[float]:
        count = self.numeric_counts.get(field, 0)
        if count == 0:
            return None
        return self.numeric_sums[field] / count

    def majority(self, field: str) -> Optional[int]:
        ones = self.binary_ones.get(field, 0)
        zeros = self.binary_zeros.get(field, 0)
        if ones == 0 and zeros == 0:
            return None
        if ones > zeros:
            return 1
        if zeros > ones:
            return 0
        return self.binary_latest[field]


class CsvAveragingRecorder:
    def __init__(
        self,
        configs: List[RecorderConfig],
        data_root: Path,
        monotonic_fn: Callable[[], float] = time.monotonic,
        utcnow_fn: Callable[[], datetime] = utc_now,
        fsync: bool = True,
    ):
        self.configs = {config.uid: config for config in configs}
        self.data_root = Path(data_root)
        self.monotonic_fn = monotonic_fn
        self.utcnow_fn = utcnow_fn
        self.fsync = fsync
        self.active = False
        self.windows: Dict[str, AveragingWindow] = {}
        self.lock = threading.RLock()
        self.diagnostics: Dict[str, Dict[str, Any]] = {
            uid: {
                "recording_enabled": config.enabled,
                "interval_s": config.interval_s,
                "mode": config.mode,
                "current_sample_count": 0,
                "current_window_start_utc": None,
                "last_write_utc": None,
                "last_file": None,
                "last_error": None,
            }
            for uid, config in self.configs.items()
        }

    def start(self) -> None:
        with self.lock:
            self.active = True
            self.windows = {}
            for uid in self.diagnostics:
                self.diagnostics[uid]["current_sample_count"] = 0
                self.diagnostics[uid]["current_window_start_utc"] = None

    def stop(self) -> None:
        with self.lock:
            self.flush_all_locked()
            self.active = False

    def add_reading(self, uid: str, reading: Dict[str, Any]) -> bool:
        with self.lock:
            config = self.configs.get(uid)
            if config is None or not config.enabled or not self.active:
                return False
            if reading.get("status") != "ok":
                return False

            window = self.windows.get(uid)
            if window is not None and window.pending_write:
                logger.warning("Recording window for %s is awaiting a retry after write failure; skipping new sample", uid)
                return False

            now_monotonic = self.monotonic_fn()
            now_utc = self.utcnow_fn()
            if window is not None and now_monotonic - window.start_monotonic >= config.interval_s:
                self._flush_window_locked(uid)
                window = self.windows.get(uid)
                if window is not None:
                    return False

            if window is None:
                window = AveragingWindow(now_monotonic, now_utc, config.driver_name)
                self.windows[uid] = window

            self._accept_sample(window, reading)
            self._update_current_diagnostics(uid, window)
            return True

    def flush_due(self) -> None:
        with self.lock:
            now_monotonic = self.monotonic_fn()
            for uid, config in self.configs.items():
                window = self.windows.get(uid)
                if window is None or window.sample_count == 0:
                    continue
                if window.pending_write or now_monotonic - window.start_monotonic >= config.interval_s:
                    self._flush_window_locked(uid)

    def flush_uid(self, uid: str) -> None:
        with self.lock:
            window = self.windows.get(uid)
            if window is not None and window.sample_count > 0:
                self._flush_window_locked(uid)

    def flush_all(self) -> None:
        with self.lock:
            self.flush_all_locked()

    def flush_all_locked(self) -> None:
        for uid in list(self.windows.keys()):
            window = self.windows.get(uid)
            if window is not None and window.sample_count > 0:
                self._flush_window_locked(uid)

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "recording_enabled": any(config.enabled for config in self.configs.values()),
                "data_root": str(self.data_root),
                "active": self.active,
                "drivers": {uid: dict(fields) for uid, fields in self.diagnostics.items()},
            }

    def _accept_sample(self, window: AveragingWindow, reading: Dict[str, Any]) -> None:
        window.sample_count += 1
        data = reading.get("data") or {}
        extended = reading.get("extended") or {}

        if window.driver_name == "spn1":
            window.add_numeric("total_w_m2", data.get("total_w_m2"))
            window.add_numeric("diffuse_w_m2", data.get("diffuse_w_m2"))
            window.add_binary("sun", data.get("sun"))
            return

        panel_1 = data.get("panel_voltage_1_v")
        if panel_1 is None:
            panel_1 = data.get("panel_voltage_v")

        window.add_numeric("panel_voltage_1_v", panel_1)
        window.add_numeric("panel_voltage_2_v", data.get("panel_voltage_2_v"))
        window.add_numeric("panel_voltage_3_v", data.get("panel_voltage_3_v"))
        window.add_numeric("panel_voltage_4_v", data.get("panel_voltage_4_v"))
        window.add_numeric("rssi_dbm", extended.get("rssi_dbm"))
        window.add_binary("voltage_ok", extended.get("voltage_ok"))

    def _flush_window_locked(self, uid: str) -> None:
        config = self.configs[uid]
        window = self.windows[uid]
        if window.pending_row is None:
            window.completed_end_utc = self.utcnow_fn()
            window.pending_row = self._build_row(config, window, window.completed_end_utc)

        end_utc = window.completed_end_utc or self.utcnow_fn()
        try:
            path = self._write_row(uid, config.driver_name, end_utc, window.pending_row)
        except Exception as exc:
            error = str(exc)
            logger.warning("CSV recording write failed for %s: %s", uid, error)
            self.diagnostics[uid]["last_error"] = error
            self._update_current_diagnostics(uid, window)
            return

        logger.info("CSV recording wrote averaged row for %s to %s", uid, path)
        self.diagnostics[uid].update(
            {
                "current_sample_count": 0,
                "current_window_start_utc": None,
                "last_write_utc": iso_utc(end_utc),
                "last_file": str(path),
                "last_error": None,
            }
        )
        del self.windows[uid]

    def _build_row(self, config: RecorderConfig, window: AveragingWindow, end_utc: datetime) -> Dict[str, str]:
        base = {
            "timestamp_utc": iso_utc(end_utc),
            "window_start_utc": iso_utc(window.start_utc),
            "window_end_utc": iso_utc(end_utc),
            "sample_count": str(window.sample_count),
        }
        if config.driver_name == "spn1":
            base.update(
                {
                    "total_w_m2": self._format_mean(window, "total_w_m2", 3),
                    "diffuse_w_m2": self._format_mean(window, "diffuse_w_m2", 3),
                    "sun": self._format_binary(window, "sun"),
                }
            )
            return base

        base.update(
            {
                "panel_voltage_1_v": self._format_mean(window, "panel_voltage_1_v", 4),
                "panel_voltage_2_v": self._format_mean(window, "panel_voltage_2_v", 4),
                "panel_voltage_3_v": self._format_mean(window, "panel_voltage_3_v", 4),
                "panel_voltage_4_v": self._format_mean(window, "panel_voltage_4_v", 4),
                "voltage_ok": self._format_binary(window, "voltage_ok"),
                "rssi_dbm": self._format_mean(window, "rssi_dbm", 1),
            }
        )
        return base

    def _format_mean(self, window: AveragingWindow, field: str, places: int) -> str:
        mean = window.mean(field)
        if mean is None:
            return ""
        return f"{mean:.{places}f}"

    def _format_binary(self, window: AveragingWindow, field: str) -> str:
        value = window.majority(field)
        if value is None:
            return ""
        return str(value)

    def _write_row(self, uid: str, driver_name: str, end_utc: datetime, row: Dict[str, str]) -> Path:
        device_dir = self.data_root / uid
        device_dir.mkdir(parents=True, exist_ok=True)
        path = device_dir / f"{end_utc.astimezone(timezone.utc).date().isoformat()}.csv"
        header = SPN1_HEADER if driver_name == "spn1" else SOLAR_HEADER
        needs_header = not path.exists() or path.stat().st_size == 0

        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        return path

    def _update_current_diagnostics(self, uid: str, window: AveragingWindow) -> None:
        self.diagnostics[uid]["current_sample_count"] = window.sample_count
        self.diagnostics[uid]["current_window_start_utc"] = iso_utc(window.start_utc)

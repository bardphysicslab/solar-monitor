import logging
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger(__name__)

DEFAULT_BACKUP_INTERVAL_MINUTES = 30.0


@dataclass
class BackupConfig:
    enabled: bool = False
    interval_minutes: float = DEFAULT_BACKUP_INTERVAL_MINUTES
    remote: str = ""
    destination: str = "solar-monitor"


def parse_backup_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def parse_backup_interval_minutes(value: Any) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid backup interval_minutes; falling back to %.0f minutes", DEFAULT_BACKUP_INTERVAL_MINUTES)
        return DEFAULT_BACKUP_INTERVAL_MINUTES

    if interval <= 0:
        logger.warning("Invalid backup interval_minutes; falling back to %.0f minutes", DEFAULT_BACKUP_INTERVAL_MINUTES)
        return DEFAULT_BACKUP_INTERVAL_MINUTES

    return interval


def backup_config_from_app_config(config: Dict[str, Any]) -> BackupConfig:
    backup = config.get("backup", {})
    return BackupConfig(
        enabled=parse_backup_enabled(backup.get("enabled", False)),
        interval_minutes=parse_backup_interval_minutes(
            backup.get("interval_minutes", DEFAULT_BACKUP_INTERVAL_MINUTES)
        ),
        remote=str(backup.get("remote", "")).strip(),
        destination=str(backup.get("destination", "solar-monitor")).strip() or "solar-monitor",
    )


class DataBackupManager:
    def __init__(
        self,
        config: BackupConfig,
        data_root: Path,
        snapshot_root: Path,
        recorder: Any,
        run_command: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.config = config
        self.data_root = Path(data_root)
        self.snapshot_root = Path(snapshot_root)
        self.recorder = recorder
        self.run_command = run_command
        self.lock = threading.RLock()
        self.last_attempt_utc: Optional[str] = None
        self.last_success_utc: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_snapshot: Optional[str] = None

    def backup_once(self, timestamp_utc: str) -> Dict[str, Any]:
        with self.lock:
            self.last_attempt_utc = timestamp_utc

            if not self.config.enabled:
                return self._status("skipped", error=None)
            if not self.config.remote:
                self.last_error = "Backup remote is not configured"
                logger.warning(self.last_error)
                return self._status("error", error=self.last_error)

            snapshot_path = None
            try:
                snapshot_path = self.create_snapshot()
                remote_destination = f"{self.config.remote}:{self.config.destination}".rstrip(":")
                self.run_command(
                    ["rclone", "sync", str(snapshot_path), remote_destination],
                    check=True,
                )
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("Solar Monitor backup failed: %s", self.last_error)
                return self._status("error", error=self.last_error)

            shutil.rmtree(snapshot_path, ignore_errors=True)
            self.last_success_utc = timestamp_utc
            self.last_error = None
            self.last_snapshot = str(snapshot_path)
            logger.info("Solar Monitor backup synchronized %s to %s", snapshot_path, remote_destination)
            return self._status("ok", error=None)

    def create_snapshot(self) -> Path:
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        snapshot_path = Path(tempfile.mkdtemp(prefix="solar-monitor-", dir=str(self.snapshot_root)))

        try:
            with self.recorder.lock:
                self.recorder.flush_due()
                if self.data_root.exists():
                    destination = snapshot_path / self.data_root.name
                    shutil.copytree(self.data_root, destination)
                else:
                    (snapshot_path / self.data_root.name).mkdir(parents=True, exist_ok=True)
        except Exception:
            shutil.rmtree(snapshot_path, ignore_errors=True)
            raise

        return snapshot_path

    def status(self) -> Dict[str, Any]:
        return self._status("ok" if self.last_error is None else "error", error=self.last_error)

    def _status(self, status: str, error: Optional[str]) -> Dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "interval_minutes": self.config.interval_minutes,
            "remote": self.config.remote,
            "destination": self.config.destination,
            "last_attempt_utc": self.last_attempt_utc,
            "last_success_utc": self.last_success_utc,
            "last_error": error,
            "last_snapshot": self.last_snapshot,
            "status": status,
        }


def render_systemd_timer(interval_minutes: float) -> str:
    minutes = parse_backup_interval_minutes(interval_minutes)
    seconds = int(round(minutes * 60))
    return "\n".join(
        [
            "[Unit]",
            "Description=Solar Monitor CSV backup timer",
            "",
            "[Timer]",
            f"OnBootSec={seconds}s",
            f"OnUnitActiveSec={seconds}s",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


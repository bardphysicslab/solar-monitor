from datetime import datetime, timezone
from typing import Any, Dict, Optional
import re
import threading
import time

try:
    import serial
except ImportError:
    serial = None


class SPN1Driver:
    def __init__(self, uid: str, port: str, baud: int):
        self.uid = uid
        self.port = port
        self.baud = baud
        self._instrument = None
        self._lock = threading.RLock()

    def get_info(self) -> dict:
        return {
            "uid": self.uid,
            "source_type": "solar_radiometer",
            "transport": "serial",
            "protocol": "SPN1",
            "firmware": None,
            "port": self.port,
            "baud": self.baud,
        }

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "total_w_m2": {"label": "Total radiation", "unit": "W/m2"},
                "diffuse_w_m2": {"label": "Diffuse radiation", "unit": "W/m2"},
                "sun": {"label": "Sun state", "unit": "boolean"},
            },
            "raw_available": False,
        }

    def get_controls(self) -> dict:
        return {
            "status": {"label": "Read SPN1 status", "dangerous": False},
            "time_read": {"label": "Read SPN1 clock", "dangerous": False},
            "time_sync": {"label": "Sync SPN1 clock to server time", "dangerous": False},
        }

    def get_reading(self) -> dict:
        try:
            line = self._send_command("S")
            parsed = self._parse_line(line)
        except Exception:
            self._close()
            parsed = None

        if parsed is None:
            return self._error_reading()

        total_w_m2, diffuse_w_m2, sun = parsed
        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "ok",
            "data": {
                "total_w_m2": total_w_m2,
                "diffuse_w_m2": diffuse_w_m2,
                "sun": sun,
            },
            "extended": {},
            "raw": None,
        }

    def get_device_status(self) -> dict:
        try:
            response = self._send_command("I")
        except Exception as exc:
            self._close()
            return self._control_error(str(exc))

        return {
            "status": "ok",
            "timestamp": self._utc_timestamp(),
            "response": response,
            "raw": response,
        }

    def get_device_time(self) -> dict:
        try:
            response = self._send_command("Z")
        except Exception as exc:
            self._close()
            return self._device_time_error(str(exc))

        parsed = self._parse_spn1_datetime(response)
        return {
            "status": "ok",
            "timestamp": self._utc_timestamp(),
            "device_time_local": self._format_device_time(parsed) if parsed else None,
            "parsed": parsed is not None,
            "raw": response,
        }

    def sync_device_time(self, dt_local: datetime) -> dict:
        before = self.get_device_time()
        date_command = f"Y{dt_local:%Y/%m/%d}"
        time_command = f"H{dt_local:%H:%M:%S}"

        try:
            with self._lock:
                test_response = self._send_command("T")
                date_response = self._send_command(date_command)
                time_response = self._send_command(time_command)
                run_response = self._send_command("R")
                verify_response = self._send_command("Z")
        except Exception as exc:
            self._close()
            return {
                "status": "error",
                "timestamp": self._utc_timestamp(),
                "before": before,
                "after": None,
                "error": str(exc),
            }

        parsed_after = self._parse_spn1_datetime(verify_response)
        after = {
            "status": "ok" if parsed_after else "error",
            "timestamp": self._utc_timestamp(),
            "device_time_local": self._format_device_time(parsed_after) if parsed_after else None,
            "parsed": parsed_after is not None,
            "raw": verify_response,
        }
        delta_seconds = (
            abs((parsed_after - dt_local.replace(tzinfo=None)).total_seconds())
            if parsed_after
            else None
        )
        verified = delta_seconds is not None and delta_seconds <= 5

        return {
            "status": "ok" if verified else "error",
            "timestamp": self._utc_timestamp(),
            "before": before,
            "after": after,
            "delta_seconds": delta_seconds,
            "responses": {
                "test_mode": test_response,
                "date_set": date_response,
                "time_set": time_response,
                "run_mode": run_response,
            },
            "error": None if verified else "SPN1 time verification mismatch",
        }

    def _send_command(self, command: str, read_timeout_s: float = 1.0) -> str:
        """
        Send one SPN1 command and return bounded decoded response.
        Use CR line ending because SPN1 manual and observed hardware use CR.
        Keep this vendor-specific behavior inside the driver.
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed")

        with self._lock:
            instrument = self._open(read_timeout_s=read_timeout_s)
            if hasattr(instrument, "reset_input_buffer"):
                instrument.reset_input_buffer()
            instrument.write(f"{command}\r".encode("ascii"))
            if hasattr(instrument, "flush"):
                instrument.flush()
            time.sleep(0.05)
            raw_line = instrument.readline(256)
            return raw_line.decode("ascii", errors="ignore").strip()

    def _open(self, read_timeout_s: float = 1.0):
        if self._instrument is None or not self._instrument.is_open:
            self._instrument = serial.Serial(self.port, self.baud, timeout=read_timeout_s)
        else:
            self._instrument.timeout = read_timeout_s
        return self._instrument

    def _close(self) -> None:
        if self._instrument is None:
            return
        try:
            self._instrument.close()
        finally:
            self._instrument = None

    def _parse_line(self, line: str) -> Optional[tuple[float, float, int]]:
        # SPN1 replies can include startup/junk bytes before or after the useful record.
        # Valid sample example: "S    1.1,    0.0,0"
        match = re.search(
            r"S\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([01])",
            line,
        )
        if not match:
            return None

        return float(match.group(1)), float(match.group(2)), int(match.group(3))

    def _parse_spn1_datetime(self, text: str) -> Optional[datetime]:
        match = re.search(
            r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})",
            text,
        )
        if not match:
            return None

        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(4)),
                int(match.group(5)),
                int(match.group(6)),
            )
        except ValueError:
            return None

    def _format_device_time(self, value: datetime) -> str:
        return value.strftime("%Y/%m/%d %H:%M:%S")

    def _control_error(self, error: str) -> dict:
        return {
            "status": "error",
            "timestamp": self._utc_timestamp(),
            "response": None,
            "raw": None,
            "error": error,
        }

    def _device_time_error(self, error: str) -> dict:
        return {
            "status": "error",
            "timestamp": self._utc_timestamp(),
            "device_time_local": None,
            "parsed": False,
            "raw": None,
            "error": error,
        }

    def _error_reading(self) -> dict:
        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "error",
            "data": {
                "total_w_m2": None,
                "diffuse_w_m2": None,
                "sun": None,
            },
            "extended": {},
            "raw": None,
        }

    def _utc_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

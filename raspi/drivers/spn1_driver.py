from datetime import datetime, timezone
from typing import Optional
import re
import threading
import time

try:
    import serial
except ImportError:
    serial = None


class SPN1Driver:
    def __init__(
        self,
        uid: str,
        port: str,
        baud: int,
        auto_sync_time: bool = True,
        sync_interval_hours: float = 24,
    ):
        self.uid = uid
        self.port = port
        self.baud = baud
        self.auto_sync_time = auto_sync_time
        self.sync_interval_hours = sync_interval_hours
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
            "auto_sync_time": self.auto_sync_time,
            "sync_interval_hours": self.sync_interval_hours,
        }

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "total_w_m2": {"label": "Total radiation", "unit": "W/m2"},
                "diffuse_w_m2": {"label": "Diffuse radiation", "unit": "W/m2"},
                "sun": {"label": "Sun state", "unit": "boolean"},
            },
            "raw_available": True,
        }

    def get_controls(self) -> dict:
        return {
            "status": {"label": "Read SPN1 status", "dangerous": False},
            "time_read": {"label": "Read SPN1 clock", "dangerous": False},
            "time_sync": {"label": "Sync SPN1 clock to server time", "dangerous": False},
        }

    def get_reading(self) -> dict:
        raw = ""
        try:
            raw = self._send_command("S", response_delay_s=0.3, read_window_s=0.8)
            parsed = self._parse_reading(raw)

            if parsed is None:
                # One retry helps after wake-up / junk bytes.
                time.sleep(0.15)
                raw = self._send_command("S", response_delay_s=0.3, read_window_s=0.8)
                parsed = self._parse_reading(raw)

        except Exception as exc:
            self._close()
            return self._error_reading(error=str(exc), raw=raw)

        if parsed is None:
            return self._error_reading(error="Could not parse SPN1 reading", raw=raw)

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
            "raw": raw,
        }

    def get_device_status(self) -> dict:
        try:
            response = self._send_command("I", response_delay_s=0.4, read_window_s=1.2)
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
            response = self._send_command("Z", response_delay_s=0.3, read_window_s=1.0)
        except Exception as exc:
            self._close()
            return self._device_time_error(str(exc))

        parsed = self._parse_spn1_datetime(response)

        return {
            "status": "ok" if parsed else "error",
            "timestamp": self._utc_timestamp(),
            "device_time_local": self._format_device_time(parsed) if parsed else None,
            "parsed": parsed is not None,
            "raw": response,
            "error": None if parsed else "Could not parse SPN1 time",
        }

    def sync_device_time(self, dt_utc: datetime) -> dict:
        """
        Sync the SPN1 clock using UTC from the Raspberry Pi system clock.

        The SPN1 protocol accepts date/time fields without timezone metadata, so
        this driver writes the UTC date and UTC time as plain SPN1 values.
        """
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        else:
            dt_utc = dt_utc.astimezone(timezone.utc)

        dt_utc_naive = dt_utc.replace(tzinfo=None)
        date_command = f"Y{dt_utc:%Y/%m/%d}"
        time_command = f"H{dt_utc:%H:%M:%S}"

        test_response = ""
        date_response = ""
        time_response = ""
        run_response = ""
        verify_response = ""
        step = "start"

        with self._lock:
            before = self.get_device_time()

            try:
                instrument = self._open(read_timeout_s=0.05)
                self._reset_input(instrument)

                step = "enter_test_mode"
                test_response = self._enter_test_mode()
                if "TEST" not in test_response:
                    return {
                        "status": "error",
                        "timestamp": self._utc_timestamp(),
                        "before": before,
                        "after": None,
                        "step": step,
                        "response_T": self._bound_debug(test_response),
                        "response_Y": "",
                        "response_H": "",
                        "response_R": "",
                        "response_Z": "",
                        "error": "SPN1 did not enter TEST mode",
                    }

                step = "Y"
                self._write_raw(f"{date_command}\r".encode("ascii"))
                date_response = self._read_until(
                    lambda text: "TEST:" in text or self._parse_spn1_datetime(text) is not None,
                    timeout_s=1.5,
                )

                step = "H"
                self._write_raw(f"{time_command}\r".encode("ascii"))
                time_response = self._read_until(
                    lambda text: "TEST:" in text or self._parse_spn1_datetime(text) is not None,
                    timeout_s=1.5,
                )

                step = "R"
                self._write_raw(b"R\r")
                run_response = self._read_available_for(0.5)

                step = "Z"
                time.sleep(0.25)
                verify_response = self._send_command("Z", response_delay_s=0.3, read_window_s=1.0)

            except Exception as exc:
                self._close()
                return {
                    "status": "error",
                    "timestamp": self._utc_timestamp(),
                    "time_basis": "UTC",
                    "before": before,
                    "after": None,
                    "step": step,
                    "response_T": self._bound_debug(test_response),
                    "response_Y": self._bound_debug(date_response),
                    "response_H": self._bound_debug(time_response),
                    "response_R": self._bound_debug(run_response),
                    "response_Z": self._bound_debug(verify_response),
                    "error": str(exc),
                }

        parsed_after = self._parse_spn1_datetime(verify_response)

        after = {
            "status": "ok" if parsed_after else "error",
            "timestamp": self._utc_timestamp(),
            "device_time_local": self._format_device_time(parsed_after) if parsed_after else None,
            "device_time_utc": self._format_device_time(parsed_after) if parsed_after else None,
            "time_basis": "UTC",
            "parsed": parsed_after is not None,
            "raw": verify_response,
        }

        delta_seconds = (
            abs((parsed_after - dt_utc_naive).total_seconds())
            if parsed_after
            else None
        )

        verified = delta_seconds is not None and delta_seconds <= 5

        return {
            "status": "ok" if verified else "error",
            "timestamp": self._utc_timestamp(),
            "time_basis": "UTC",
            "before": before,
            "after": after,
            "delta_seconds": delta_seconds,
            "responses": {
                "test_mode": self._bound_debug(test_response),
                "date_set": self._bound_debug(date_response),
                "time_set": self._bound_debug(time_response),
                "run_mode": self._bound_debug(run_response),
                "verify": self._bound_debug(verify_response),
            },
            "error": None if verified else "SPN1 time verification mismatch",
        }

    def probe_test_mode_entry(self) -> dict:
        try:
            with self._lock:
                instrument = self._open(read_timeout_s=0.05)
                self._reset_input(instrument)

                self._write_raw(b"R\r")
                response_r = self._read_available_for(0.6)

                response_t = self._enter_test_mode()

        except Exception as exc:
            self._close()
            return {
                "status": "error",
                "timestamp": self._utc_timestamp(),
                "response_R": "",
                "response_T": "",
                "error": str(exc),
            }

        return {
            "status": "ok" if "TEST" in response_t else "error",
            "timestamp": self._utc_timestamp(),
            "response_R": self._bound_debug(response_r),
            "response_T": self._bound_debug(response_t),
        }

    # ---------- Serial helpers ----------

    def _send_command(
        self,
        command: str,
        response_delay_s: float = 0.25,
        read_window_s: float = 0.75,
        read_timeout_s: float = 0.05,
    ) -> str:
        """
        Send one SPN1 RS232 command.

        SPN1 responses can contain junk/wake-up bytes before the useful ASCII.
        So we do not rely on readline(). We read a short time window and let
        the parser search inside the whole response.
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed")

        with self._lock:
            instrument = self._open(read_timeout_s=read_timeout_s)
            self._reset_input(instrument)

            instrument.write(f"{command}\r".encode("ascii"))
            if hasattr(instrument, "flush"):
                instrument.flush()

            time.sleep(response_delay_s)

            return self._read_available_for(read_window_s)

    def _enter_test_mode(self) -> str:
        self._write_raw(b"T\r")
        return self._read_until(lambda text: "TEST" in text, timeout_s=2.5)

    def _write_raw(self, data: bytes) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed")

        instrument = self._open(read_timeout_s=0.05)
        instrument.write(data)
        if hasattr(instrument, "flush"):
            instrument.flush()

    def _read_until(self, predicate, timeout_s: float) -> str:
        deadline = time.monotonic() + timeout_s
        chunks = []

        while time.monotonic() < deadline:
            chunk = self._read_available_chunk()
            if chunk:
                chunks.append(chunk)
                text = "".join(chunks)
                if predicate(text):
                    return self._bound_debug(text)
            else:
                time.sleep(0.03)

        return self._bound_debug("".join(chunks))

    def _read_available_for(self, duration_s: float) -> str:
        deadline = time.monotonic() + duration_s
        chunks = []

        while time.monotonic() < deadline:
            chunk = self._read_available_chunk()
            if chunk:
                chunks.append(chunk)
            else:
                time.sleep(0.03)

        return self._bound_debug("".join(chunks))

    def _read_available_chunk(self) -> str:
        instrument = self._open(read_timeout_s=0.05)
        waiting = getattr(instrument, "in_waiting", 0) or 0
        size = max(1, min(waiting, 256))
        data = instrument.read(size)
        return data.decode("ascii", errors="ignore")

    def _open(self, read_timeout_s: float = 1.0):
        if serial is None:
            raise RuntimeError("pyserial is not installed")

        if self._instrument is None or not self._instrument.is_open:
            self._instrument = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=read_timeout_s,
                write_timeout=1.0,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )

            # SPN1 may use DTR power on real RS232 adapters.
            try:
                self._instrument.setDTR(True)
            except Exception:
                pass

        else:
            self._instrument.timeout = read_timeout_s

        return self._instrument

    def _reset_input(self, instrument) -> None:
        if hasattr(instrument, "reset_input_buffer"):
            instrument.reset_input_buffer()

    def _close(self) -> None:
        if self._instrument is None:
            return

        try:
            self._instrument.close()
        finally:
            self._instrument = None

    # ---------- Parsers ----------

    def _parse_reading(self, text: str) -> Optional[tuple[float, float, int]]:
        """
        Accept both:
          S   11.1,    8.8,0
        and:
          11.1,    8.8,0

        Also tolerates junk before/after.
        """
        match = re.search(
            r"(?:^|S|\s)([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([01])",
            text,
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

    # ---------- Formatting / errors ----------

    def _format_device_time(self, value: datetime) -> str:
        return value.strftime("%Y/%m/%d %H:%M:%S")

    def _bound_debug(self, text: str, limit: int = 500) -> str:
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _error_reading(self, error: str = "SPN1 read failed", raw: str = "") -> dict:
        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "error",
            "data": {
                "total_w_m2": None,
                "diffuse_w_m2": None,
                "sun": None,
            },
            "extended": {
                "error": error,
            },
            "raw": self._bound_debug(raw),
        }

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

    def _utc_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

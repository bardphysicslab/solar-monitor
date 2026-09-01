from datetime import datetime, timezone
import math
import threading
import time
from typing import Callable, Iterable, Optional

try:
    import serial
except ImportError:
    serial = None


CHANNELS = ("voltage_v", "current_a", "power_w", "load_resistance_ohm")
ACKNOWLEDGMENT = "execu success"
COMMAND_ERROR = "cmd err"


class ET54Error(RuntimeError):
    """Base error for ET54 operations."""


class ET54TransportError(ET54Error):
    """The serial instrument could not be reached or did not respond."""


class ET54ProtocolError(ET54Error):
    """The instrument returned an error or an invalid response."""


class ET54Driver:
    def __init__(
        self,
        uid: str,
        port: str,
        baud: int = 9600,
        mode: str = "CR",
        resistance_ohm: float = 100.0,
        timeout_s: float = 1.0,
        serial_factory: Optional[Callable[..., object]] = None,
    ):
        self.uid = uid
        self.port = port
        self.baud = int(baud)
        self.mode = str(mode).upper()
        self.resistance_ohm = self._validate_resistance(resistance_ohm)
        self.timeout_s = float(timeout_s)
        self._serial_factory = serial_factory
        self._instrument = None
        self._lock = threading.RLock()
        self._last_seen = None

        if self.mode != "CR":
            raise ValueError("ET54 currently supports only CR mode")

    def get_info(self) -> dict:
        try:
            resistance_setpoint_ohm = self.get_resistance()
        except Exception:
            resistance_setpoint_ohm = None

        return {
            "uid": self.uid,
            "manufacturer": "East Tester",
            "model": "ET5406A+",
            "source_type": "electronic_load",
            "driver": "et54",
            "transport": "usb_serial",
            "protocol": "ET54 serial commands",
            "port": self.port,
            "baud": self.baud,
            "configured_mode": self.mode,
            "resistance_setpoint_ohm": resistance_setpoint_ohm,
        }

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "voltage_v": {"label": "Voltage", "unit": "V"},
                "current_a": {"label": "Current", "unit": "A"},
                "power_w": {"label": "Electrical power", "unit": "W"},
                "load_resistance_ohm": {
                    "label": "Applied electronic-load resistance",
                    "unit": "ohm",
                },
            },
            "features": {
                "constant_resistance": True,
                "input_control": True,
                "resistance_sweep": True,
            },
            "raw_available": True,
        }

    def close(self) -> None:
        with self._lock:
            self._close()

    def identify(self) -> str:
        return self._query("*IDN?")

    def get_mode(self) -> str:
        response = self._query("CH:MODE?").strip().upper()
        if response not in {"CC", "CV", "CR", "CP"}:
            raise ET54ProtocolError(f"Invalid ET54 mode response: {response!r}")
        return response

    def set_mode_cr(self) -> None:
        self._set("CH:MODE CR")

    def get_resistance(self) -> float:
        response = self._query("RESI:CR?")
        try:
            value = float(response)
        except ValueError as exc:
            raise ET54ProtocolError(f"Invalid ET54 resistance response: {response!r}") from exc
        return self._validate_resistance(value)

    def set_resistance(self, ohms: float) -> None:
        value = self._validate_resistance(ohms)
        self._set(f"RESI:CR {value:g}")

    def input_state(self) -> bool:
        response = self._query("CH:SW?").strip().upper()
        if response in {"ON", "1"}:
            return True
        if response in {"OFF", "0"}:
            return False
        raise ET54ProtocolError(f"Invalid ET54 input-state response: {response!r}")

    def input_on(self) -> None:
        self._set("CH:SW ON")

    def input_off(self) -> None:
        self._set("CH:SW OFF")

    def measure_all(self) -> dict:
        raw = self._query("MEAS:ALL?")
        fields = raw.split()
        if len(fields) != 4:
            raise ET54ProtocolError(f"Invalid MEAS:ALL? field count: {raw!r}")
        try:
            current, voltage, power, resistance = (float(field) for field in fields)
        except ValueError as exc:
            raise ET54ProtocolError(f"Invalid MEAS:ALL? response: {raw!r}") from exc
        values = (current, voltage, power, resistance)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ET54ProtocolError(f"Invalid MEAS:ALL? values: {raw!r}")
        self._last_seen = self._utc_timestamp()
        return {
            "current_a": current,
            "voltage_v": voltage,
            "power_w": power,
            "load_resistance_ohm": resistance,
        }

    def get_reading(self) -> dict:
        raw = ""
        try:
            raw = self._exchange("MEAS:ALL?")
            measurements = self._parse_measure_all_response(raw)
            self._last_seen = self._utc_timestamp()
        except ET54TransportError as exc:
            return self._error_reading("node_unavailable", str(exc), raw)
        except (ET54ProtocolError, ValueError) as exc:
            return self._error_reading("error", str(exc), raw)

        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "ok",
            "message": "Fresh valid reading",
            "data": measurements,
            "extended": {"last_seen": self._last_seen, "port": self.port},
            "raw": self._bound_debug(raw),
        }

    def sweep_resistance(self, values: Iterable[float], settle_s: float = 0.5) -> dict:
        requested = [self._validate_resistance(value) for value in values]
        if not requested:
            raise ValueError("Resistance sweep requires at least one value")
        settle = float(settle_s)
        if not math.isfinite(settle) or settle < 0:
            raise ValueError("settle_s must be a finite non-negative number")

        safe_start = max(requested)
        points = []
        started_at = self._utc_timestamp()
        off_error = None

        with self._lock:
            try:
                if self.get_mode() != "CR":
                    self.set_mode_cr()
                self.set_resistance(safe_start)
                self.input_on()
                for resistance in requested:
                    self.set_resistance(resistance)
                    if settle:
                        time.sleep(settle)
                    measurement = self.measure_all()
                    points.append(
                        {
                            "timestamp": self._utc_timestamp(),
                            "set_resistance_ohm": resistance,
                            **measurement,
                        }
                    )
            finally:
                try:
                    self.input_off()
                except Exception as exc:
                    off_error = str(exc)

        return {
            "uid": self.uid,
            "mode": "CR",
            "started_at": started_at,
            "completed_at": self._utc_timestamp(),
            "settle_s": settle,
            "safe_start_resistance_ohm": safe_start,
            "requested_resistance_ohm": requested,
            "points": points,
            "input_off_error": off_error,
        }

    def _parse_measure_all_response(self, response: str) -> dict:
        payload = self._strip_response_prefix(response)
        fields = payload.split()
        if len(fields) != 4:
            raise ET54ProtocolError(f"Invalid MEAS:ALL? field count: {response!r}")
        try:
            current, voltage, power, resistance = (float(field) for field in fields)
        except ValueError as exc:
            raise ET54ProtocolError(f"Invalid MEAS:ALL? response: {response!r}") from exc
        values = (current, voltage, power, resistance)
        if not all(math.isfinite(value) and value >= 0 for value in values):
            raise ET54ProtocolError(f"Invalid MEAS:ALL? values: {response!r}")
        return {
            "current_a": current,
            "voltage_v": voltage,
            "power_w": power,
            "load_resistance_ohm": resistance,
        }

    def _query(self, command: str) -> str:
        return self._strip_response_prefix(self._exchange(command))

    def _set(self, command: str) -> None:
        response = self._strip_response_prefix(self._exchange(command))
        if response.lower() != ACKNOWLEDGMENT:
            raise ET54ProtocolError(f"Unexpected acknowledgment for {command}: {response!r}")

    def _exchange(self, command: str) -> str:
        with self._lock:
            try:
                instrument = self._open()
                instrument.write(f"{command}\n".encode("ascii"))
                if hasattr(instrument, "flush"):
                    instrument.flush()
                response = instrument.readline()
            except Exception as exc:
                self._close()
                raise ET54TransportError(f"ET54 serial command failed ({command}): {exc}") from exc

            if not response:
                self._close()
                raise ET54TransportError(f"ET54 did not respond to {command}")
            text = response.decode("ascii", errors="replace").strip()
            if not text:
                self._close()
                raise ET54TransportError(f"ET54 returned an empty response to {command}")
            if COMMAND_ERROR in self._strip_response_prefix(text).lower():
                raise ET54ProtocolError(f"ET54 rejected command {command}: {text}")
            return self._bound_debug(text)

    def _open(self):
        if self._instrument is not None and getattr(self._instrument, "is_open", True):
            return self._instrument
        factory = self._serial_factory
        if factory is None:
            if serial is None:
                raise RuntimeError("pyserial is not installed")
            factory = serial.Serial
        kwargs = {
            "port": self.port,
            "baudrate": self.baud,
            "timeout": self.timeout_s,
            "write_timeout": self.timeout_s,
        }
        if serial is not None:
            kwargs.update(
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
        self._instrument = factory(**kwargs)
        if hasattr(self._instrument, "reset_input_buffer"):
            self._instrument.reset_input_buffer()
        return self._instrument

    def _close(self) -> None:
        if self._instrument is None:
            return
        try:
            self._instrument.close()
        finally:
            self._instrument = None

    @staticmethod
    def _strip_response_prefix(response: str) -> str:
        text = response.strip()
        return text[1:].strip() if text.startswith("R") else text

    @staticmethod
    def _validate_resistance(value: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Resistance must be numeric") from exc
        if not math.isfinite(result) or result <= 0:
            raise ValueError("Resistance must be a finite positive number")
        return result

    @staticmethod
    def _bound_debug(text: str, limit: int = 500) -> str:
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _error_reading(self, status: str, error: str, raw: str) -> dict:
        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": status,
            "message": error,
            "data": {channel: None for channel in CHANNELS},
            "extended": {"error": error, "last_seen": self._last_seen, "port": self.port},
            "raw": self._bound_debug(raw),
        }

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

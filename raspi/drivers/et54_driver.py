from contextlib import contextmanager
from datetime import datetime, timezone
import math
import logging
import os
import threading
import time
from typing import Callable, Iterable, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


CHANNELS = ("voltage_v", "current_a", "power_w", "load_resistance_ohm")
ACKNOWLEDGMENT = "execu success"
COMMAND_ERROR = "cmd err"
UNAVAILABLE_RESISTANCE_SENTINEL_OHM = 99999999.0
logger = logging.getLogger(__name__)


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
        port_lister: Optional[Callable[[], Iterable[object]]] = None,
        reconnect_interval_s: float = 5.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self.uid = uid
        self.configured_port = port
        self.port = port
        self.baud = int(baud)
        self.mode = str(mode).upper()
        self.resistance_ohm = self._validate_resistance(resistance_ohm)
        self.timeout_s = float(timeout_s)
        self._serial_factory = serial_factory
        self._port_lister = port_lister
        self._instrument = None
        self._lock = threading.RLock()
        self._last_seen = None
        self._monotonic = monotonic_fn
        self._reconnect_interval_s = max(0.0, float(reconnect_interval_s))
        self._last_reconnect_attempt = None
        self.transport_state = "disconnected"
        self.transport_message = "ET54 USB disconnected"
        self._identity_required = serial_factory is None
        self.requires_safe_off_confirmation = self._identity_required
        self.safe_off_confirmed = False

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
            "configured_port": self.configured_port,
            "baud": self.baud,
            "transport_state": self.transport_state,
            "transport_message": self.transport_message,
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
            "limits": {
                "absolute": {
                    "max_voltage_v": 120.0,
                    "max_current_a": 20.0,
                    "max_power_w": 200.0,
                    "min_load_resistance_ohm": 0.05,
                    "max_load_resistance_ohm": 4500.0,
                }
            },
            "raw_available": True,
        }

    def close(self) -> None:
        with self._lock:
            self._close()

    @contextmanager
    def operation(self):
        with self._lock:
            yield self

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
        self.safe_off_confirmed = False

    def input_off(self) -> None:
        self._set("CH:SW OFF")

    def ensure_safe_off(self) -> None:
        if self._instrument is None or self._identity_required:
            self._reconnect(force=True)
            if self.safe_off_confirmed:
                return
        self.input_off()
        if self.input_state():
            raise ET54ProtocolError("ET54 input remained ON after CH:SW OFF")
        self.transport_state = "connected"
        self.transport_message = "ET54 connected; load confirmed OFF"
        self.requires_safe_off_confirmation = False
        self.safe_off_confirmed = True

    def measure_all(self) -> dict:
        raw = self._query("MEAS:ALL?")
        measurements = self._parse_measure_all_response(raw)
        self._last_seen = self._utc_timestamp()
        return measurements

    def get_reading(self) -> dict:
        try:
            measurements = self.measure_all()
        except ET54TransportError as exc:
            return self._error_reading("node_unavailable", str(exc), "")
        except (ET54ProtocolError, ValueError) as exc:
            return self._error_reading("error", str(exc), "")

        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "ok",
            "message": "Fresh valid reading",
            "data": measurements,
            "extended": {"last_seen": self._last_seen, "port": self.port},
            "raw": "",
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
        if not all(math.isfinite(value) and value >= 0 for value in (current, voltage, power, resistance)):
            raise ET54ProtocolError(f"Invalid MEAS:ALL? values: {response!r}")
        normalized_resistance = None if resistance >= UNAVAILABLE_RESISTANCE_SENTINEL_OHM else resistance
        return {
            "current_a": current,
            "voltage_v": voltage,
            "power_w": power,
            "load_resistance_ohm": normalized_resistance,
        }

    def _query(self, command: str) -> str:
        if self._identity_required:
            self._reconnect()
        try:
            return self._strip_response_prefix(self._exchange_once(command))
        except ET54TransportError:
            self._reconnect()
            return self._strip_response_prefix(self._exchange_once(command))

    def _set(self, command: str) -> None:
        if self._identity_required:
            raise ET54TransportError("ET54 identity must be reverified before state-changing commands")
        response = self._strip_response_prefix(self._exchange_once(command))
        if response.lower() != ACKNOWLEDGMENT:
            raise ET54ProtocolError(f"Unexpected acknowledgment for {command}: {response!r}")

    def _exchange_once(self, command: str) -> str:
        with self._lock:
            try:
                instrument = self._open()
                instrument.write(f"{command}\n".encode("ascii"))
                if hasattr(instrument, "flush"):
                    instrument.flush()
                response = instrument.readline()
            except self._transport_exception_types() as exc:
                self._invalidate_transport(exc)
                raise ET54TransportError(f"ET54 serial command failed ({command}): {exc}") from exc

            if not response:
                self._invalidate_transport()
                raise ET54TransportError(f"ET54 did not respond to {command}")
            text = response.decode("ascii", errors="replace").strip()
            if not text:
                self._invalidate_transport()
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
        self.transport_state = "connected"
        self.transport_message = "ET54 USB connected"
        return self._instrument

    def _reconnect(self, force: bool = False) -> None:
        with self._lock:
            now = self._monotonic()
            if not force and self._last_reconnect_attempt is not None and now - self._last_reconnect_attempt < self._reconnect_interval_s:
                raise ET54TransportError("ET54 reconnect is waiting for the bounded retry interval")
            self._last_reconnect_attempt = now
            self.transport_state = "reconnecting"
            self.transport_message = "Reconnecting to ET5406A+"
            self._close()
            candidates = self._candidate_ports()
            if not candidates:
                self._invalidate_transport()
                raise ET54TransportError("No matching ET5406A+ USB serial device found")
            if len(candidates) > 1:
                self._invalidate_transport()
                raise ET54TransportError("Multiple matching ET5406A+ USB serial devices found; reconnect is ambiguous")
            candidate = candidates[0]
            previous_port = self.port
            self.port = candidate
            try:
                self._open()
                identity = self._strip_response_prefix(self._exchange_once("*IDN?"))
                if "ET5406A+" not in identity.upper():
                    raise ET54ProtocolError(f"Unexpected USB serial device identity: {identity!r}")
                self._identity_required = False
                off_response = self._strip_response_prefix(self._exchange_once("CH:SW OFF"))
                if off_response.lower() != ACKNOWLEDGMENT:
                    raise ET54ProtocolError(f"Unexpected acknowledgment for CH:SW OFF: {off_response!r}")
                input_response = self._strip_response_prefix(self._exchange_once("CH:SW?"))
                if input_response.strip().upper() not in {"OFF", "0"}:
                    raise ET54ProtocolError(f"ET54 input OFF could not be confirmed: {input_response!r}")
            except self._transport_exception_types() as exc:
                self._close()
                self.port = previous_port
                self.transport_state = "fault"
                self.transport_message = "ET54 identity verification failed"
                raise ET54TransportError(f"Could not reconnect to ET5406A+: {exc}") from exc
            except (ET54TransportError, ET54ProtocolError):
                self._close()
                self.port = previous_port
                self.transport_state = "fault"
                self.transport_message = "ET54 identity verification failed"
                raise
            self.transport_state = "connected"
            self.transport_message = "ET54 reconnected; load confirmed OFF"
            self._identity_required = False
            self.requires_safe_off_confirmation = False
            self.safe_off_confirmed = True

    def _candidate_ports(self) -> list:
        if self._serial_factory is not None and self._port_lister is None:
            return [self.configured_port]
        configured_exists = bool(self.configured_port and os.path.exists(self.configured_port))
        if configured_exists:
            return [self.configured_port]
        lister = self._port_lister or (list_ports.comports if list_ports is not None else None)
        if lister is None:
            return []
        matches = []
        for item in lister():
            device = getattr(item, "device", None)
            description = " ".join(
                str(value or "") for value in (getattr(item, "description", ""), getattr(item, "manufacturer", ""), getattr(item, "product", ""))
            ).upper()
            vid = getattr(item, "vid", None)
            pid = getattr(item, "pid", None)
            known_ch340 = vid == 0x1A86 and pid in {0x5523, 0x7523}
            if device and (known_ch340 or "CH340" in description or "USB-SERIAL" in description):
                matches.append(device)
        return sorted(set(matches))

    def _invalidate_transport(self, exc: Optional[BaseException] = None) -> None:
        if exc is not None:
            logger.warning("ET54 USB transport invalidated: %s", exc)
        self._close()
        self._identity_required = True
        self.requires_safe_off_confirmation = True
        self.safe_off_confirmed = False
        self.transport_state = "disconnected"
        self.transport_message = "ET54 USB disconnected"

    @staticmethod
    def _transport_exception_types():
        if serial is None:
            return (OSError,)
        return (OSError, serial.SerialException)

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

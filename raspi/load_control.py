from __future__ import annotations

import math
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Dict, Iterable, List, Optional, Protocol


SUPPORTED_MODES = {"fixed_resistance", "sweep"}
REQUIRED_ELECTRICAL_LIMITS = ("max_voltage_v", "max_current_a", "max_power_w")


class ElectronicLoadDriver(Protocol):
    uid: str

    def operation(self) -> ContextManager[Any]: ...
    def get_mode(self) -> str: ...
    def set_mode_cr(self) -> None: ...
    def set_resistance(self, ohms: float) -> None: ...
    def input_state(self) -> bool: ...
    def input_on(self) -> None: ...
    def input_off(self) -> None: ...
    def measure_all(self) -> dict: ...
    def get_reading(self) -> dict: ...


class LoadControlError(RuntimeError):
    pass


class SafetyConfigurationError(LoadControlError):
    pass


class SafetyViolation(LoadControlError):
    pass


class OperationStopped(LoadControlError):
    pass


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def resistance_step(value_ohm: float, step_ohm: float, direction: int, minimum: float, maximum: float) -> float:
    value = float(value_ohm)
    step = float(step_ohm)
    if step not in {10.0, 100.0, 1000.0}:
        raise ValueError("Manual resistance steps must be 10, 100, or 1000 ohm")
    if direction not in {-1, 1}:
        raise ValueError("Direction must be -1 or 1")
    return min(maximum, max(minimum, round(value + direction * step, 6)))


def _limit(profile: Dict[str, Any], name: str) -> Optional[float]:
    return numeric((profile or {}).get(name))


def _validate_profile(owner: str, limits: Dict[str, Any]) -> List[str]:
    errors = []
    for level in ("absolute", "operating"):
        profile = limits.get(level) or {}
        for name in REQUIRED_ELECTRICAL_LIMITS:
            value = _limit(profile, name)
            if value is None or value <= 0:
                errors.append(f"{owner} {level}.{name} is required")
    return errors


def effective_safety_envelope(panel_limits: Dict[str, Any], load_limits: Dict[str, Any], mode_config: Dict[str, Any]) -> Dict[str, float]:
    errors = _validate_profile("panel", panel_limits) + _validate_profile("load", load_limits)
    load_absolute = load_limits.get("absolute") or {}
    load_operating = load_limits.get("operating") or {}
    maximum_candidates = [
        _limit(load_absolute, "max_load_resistance_ohm"),
        _limit(load_operating, "max_load_resistance_ohm"),
        numeric(mode_config.get("max_load_resistance_ohm")),
    ]
    maximum_candidates = [value for value in maximum_candidates if value is not None and value > 0]
    if not maximum_candidates:
        errors.append("load resistance maximum is required")

    panel_absolute = panel_limits.get("absolute") or {}
    panel_operating = panel_limits.get("operating") or {}
    max_voltage = min(
        _limit(panel_absolute, "max_voltage_v") or math.inf,
        _limit(panel_operating, "max_voltage_v") or math.inf,
        _limit(load_absolute, "max_voltage_v") or math.inf,
        _limit(load_operating, "max_voltage_v") or math.inf,
    )
    max_current = min(
        _limit(panel_absolute, "max_current_a") or math.inf,
        _limit(panel_operating, "max_current_a") or math.inf,
        _limit(load_absolute, "max_current_a") or math.inf,
        _limit(load_operating, "max_current_a") or math.inf,
    )
    max_power = min(
        _limit(panel_absolute, "max_power_w") or math.inf,
        _limit(panel_operating, "max_power_w") or math.inf,
        _limit(load_absolute, "max_power_w") or math.inf,
        _limit(load_operating, "max_power_w") or math.inf,
    )
    minimum_candidates = [
        _limit(load_absolute, "min_load_resistance_ohm"),
        _limit(load_operating, "min_load_resistance_ohm"),
        numeric(mode_config.get("min_load_resistance_ohm")),
    ]
    if all(math.isfinite(value) and value > 0 for value in (max_voltage, max_current, max_power)):
        minimum_candidates.extend((max_voltage / max_current, max_voltage * max_voltage / max_power))
    minimum_candidates = [value for value in minimum_candidates if value is not None and value > 0]
    if not minimum_candidates:
        errors.append("safe load resistance minimum cannot be determined")

    if errors:
        raise SafetyConfigurationError("; ".join(errors))

    minimum = max(minimum_candidates)
    maximum = min(maximum_candidates)
    if minimum > maximum:
        raise SafetyConfigurationError("effective minimum resistance exceeds effective maximum")

    return {
        "max_voltage_v": max_voltage,
        "max_current_a": max_current,
        "max_power_w": max_power,
        "min_load_resistance_ohm": minimum,
        "max_load_resistance_ohm": maximum,
    }


class ElectronicLoadController:
    def __init__(
        self,
        driver: ElectronicLoadDriver,
        load_config: Dict[str, Any],
        panel_config: Optional[Dict[str, Any]],
        irradiance_provider: Optional[Callable[[str, str], Iterable[float]]] = None,
        sweep_result_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.driver = driver
        self.uid = driver.uid
        self.config = load_config
        self.panel_config = panel_config
        self.panel_uid = load_config.get("panel_uid")
        self.load_type = load_config.get("load_type", "electronic_load")
        self.irradiance_provider = irradiance_provider or (lambda _start, _end: [])
        self.sweep_result_sink = sweep_result_sink
        self.sleep_fn = sleep_fn
        self._lock = threading.RLock()
        self.selected_mode = load_config.get("default_selected_mode", "fixed_resistance")
        self.selected_resistance_ohm = float(
            (load_config.get("cr") or {}).get("default_resistance_ohm", load_config.get("resistance_ohm", 100))
        )
        self.active_mode = None
        self.input_enabled = False
        self.safety_state = "disabled"
        self.safety_message = "Load disabled"
        self.sweep_state = "idle"
        self.last_sweep = None
        self.last_measurement = None
        self._stop_requested = threading.Event()
        self._refresh_readiness()

    def _refresh_readiness(self) -> None:
        try:
            self.safety_envelope()
        except SafetyConfigurationError as exc:
            self.safety_state = "unavailable"
            self.safety_message = str(exc)
        else:
            if self.safety_state not in {"active", "safety_fault", "instrument_error", "sweep_running"}:
                self.safety_state = "ready"
                self.safety_message = "Safety configuration complete"

    def safety_envelope(self, mode: Optional[str] = None) -> Dict[str, float]:
        if not self.panel_uid:
            raise SafetyConfigurationError("load config.panel_uid is required")
        if self.panel_config is None:
            raise SafetyConfigurationError(f"associated panel does not exist: {self.panel_uid}")
        load_limits = self.config.get("limits") or {}
        panel_limits = (self.panel_config.get("config") or {}).get("limits") or {}
        mode_name = mode or self.selected_mode
        mode_config = self.config.get("cr" if mode_name == "fixed_resistance" else "sweep") or {}
        return effective_safety_envelope(panel_limits, load_limits, mode_config)

    def state(self) -> Dict[str, Any]:
        with self._lock:
            try:
                envelope = self.safety_envelope()
                config_complete = True
                config_error = None
            except SafetyConfigurationError as exc:
                envelope = None
                config_complete = False
                config_error = str(exc)
            return {
                "uid": self.uid,
                "panel_uid": self.panel_uid,
                "load_type": self.load_type,
                "device": "ET5406A+",
                "selected_mode": self.selected_mode,
                "active_mode": self.active_mode,
                "input_enabled": self.input_enabled,
                "safety_state": self.safety_state,
                "safety_message": self.safety_message,
                "resistance_setpoint_ohm": self.selected_resistance_ohm,
                "sweep_state": self.sweep_state,
                "last_measurement": self.last_measurement,
                "last_sweep": self.last_sweep,
                "safety_config_complete": config_complete,
                "safety_config_error": config_error,
                "effective_limits": envelope,
            }

    def select_mode(self, mode: str, resistance_ohm: Optional[float] = None) -> Dict[str, Any]:
        if mode not in SUPPORTED_MODES:
            raise LoadControlError(f"Unsupported load mode: {mode}")
        with self._lock:
            if self.active_mode is not None:
                raise LoadControlError("Disable the active load before selecting a mode")
            self.selected_mode = mode
            if resistance_ohm is not None:
                self._validate_resistance(float(resistance_ohm), mode="fixed_resistance")
                self.selected_resistance_ohm = float(resistance_ohm)
            self._refresh_readiness()
            return self.state()

    def select_resistance_step(self, step_ohm: float, direction: int) -> Dict[str, Any]:
        with self._lock:
            envelope = self.safety_envelope("fixed_resistance")
            selected = resistance_step(
                self.selected_resistance_ohm,
                step_ohm,
                direction,
                envelope["min_load_resistance_ohm"],
                envelope["max_load_resistance_ohm"],
            )
            self.selected_resistance_ohm = selected
            return self.state()

    def enable_fixed(self) -> Dict[str, Any]:
        with self._lock:
            if self.active_mode is not None or self.safety_state in {"safety_fault", "instrument_error"}:
                raise LoadControlError("Load must be disabled and fault-cleared before enabling")
            envelope = self.safety_envelope("fixed_resistance")
            self._validate_resistance(self.selected_resistance_ohm, envelope=envelope)
            try:
                with self.driver.operation():
                    self.driver.input_off()
                    self.driver.get_mode()
                    self.driver.input_state()
                    self.driver.set_mode_cr()
                    self.driver.set_resistance(self.selected_resistance_ohm)
                    open_measurement = self.driver.measure_all()
                    self._validate_measurement(open_measurement, envelope)
                    self.driver.input_on()
                    measurement = self.driver.measure_all()
                    self._validate_measurement(measurement, envelope)
            except Exception as exc:
                self._safe_off()
                if isinstance(exc, SafetyViolation):
                    self._latch_fault("safety_fault", str(exc))
                else:
                    self._latch_fault("instrument_error", str(exc))
                raise
            self.active_mode = "fixed_resistance"
            self.input_enabled = True
            self.safety_state = "active"
            self.safety_message = "Fixed resistance load active"
            self.last_measurement = measurement
            return self.state()

    def apply_fixed(self, resistance_ohm: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            if self.active_mode != "fixed_resistance" or not self.input_enabled:
                raise LoadControlError("Fixed resistance mode is not active")
            if resistance_ohm is not None:
                self._validate_resistance(float(resistance_ohm), mode="fixed_resistance")
                self.selected_resistance_ohm = float(resistance_ohm)
            envelope = self.safety_envelope("fixed_resistance")
            self._validate_resistance(self.selected_resistance_ohm, envelope=envelope)
            try:
                with self.driver.operation():
                    self.driver.set_resistance(self.selected_resistance_ohm)
                    measurement = self.driver.measure_all()
                    self._validate_measurement(measurement, envelope)
            except Exception as exc:
                self._safe_off()
                self._latch_fault("safety_fault" if isinstance(exc, SafetyViolation) else "instrument_error", str(exc))
                raise
            self.last_measurement = measurement
            return self.state()

    def disable(self, clear_fault: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._stop_requested.set()
            if self.input_enabled or self.active_mode is not None or self.sweep_state == "running":
                self._safe_off()
            self.active_mode = None
            self.input_enabled = False
            self.sweep_state = "idle" if self.sweep_state == "running" else self.sweep_state
            if clear_fault:
                self.safety_state = "disabled"
                self.safety_message = "Fault cleared; load disabled"
                self._refresh_readiness()
            elif self.safety_state not in {"safety_fault", "instrument_error"}:
                self.safety_state = "disabled"
                self.safety_message = "Load disabled"
            return self.state()

    def poll_reading(self) -> Dict[str, Any]:
        with self._lock:
            if self.sweep_state == "running":
                raise LoadControlError("Ordinary polling is suspended during sweep")
            reading = self.driver.get_reading()
            if reading.get("status") == "ok":
                measurement = reading.get("data") or {}
                self.last_measurement = measurement
                if self.active_mode == "fixed_resistance" and self.input_enabled:
                    try:
                        self._validate_measurement(measurement, self.safety_envelope("fixed_resistance"))
                    except SafetyViolation as exc:
                        self._safe_off()
                        self._latch_fault("safety_fault", str(exc))
                        reading = self._fault_reading(reading, str(exc))
                elif self.safety_state == "unavailable":
                    self._refresh_readiness()
            else:
                error = reading.get("message") or (reading.get("extended") or {}).get("error") or "ET54 reading failed"
                if self.active_mode is not None or self.input_enabled:
                    self._safe_off()
                    self._latch_fault("instrument_error", error)
                elif self.safety_state not in {"safety_fault", "instrument_error"}:
                    self.safety_state = "unavailable"
                    self.safety_message = error
            self._add_provenance(reading)
            return reading

    def validate_sweep_configuration(self):
        envelope = self.safety_envelope("sweep")
        sweep_config = self.config.get("sweep") or {}
        try:
            values = [float(value) for value in sweep_config.get("resistance_values_ohm", [])]
        except (TypeError, ValueError) as exc:
            raise SafetyConfigurationError("sweep resistance values must be numeric") from exc
        if not values:
            raise SafetyConfigurationError("sweep.resistance_values_ohm is required")
        if values != sorted(values, reverse=True):
            raise SafetyConfigurationError("sweep resistance values must be high-to-low")
        for value in values:
            self._validate_resistance(value, envelope=envelope)
        settle_s = numeric(sweep_config.get("settle_s"))
        if settle_s is None or settle_s < 0:
            raise SafetyConfigurationError("sweep.settle_s must be a non-negative number")
        return envelope, values, settle_s

    def run_sweep(self) -> Dict[str, Any]:
        with self._lock:
            if self.active_mode is not None or self.safety_state in {"safety_fault", "instrument_error"}:
                raise LoadControlError("Load must be disabled and fault-cleared before sweep")
            envelope, values, settle_s = self.validate_sweep_configuration()
            self.selected_mode = "sweep"
            self.active_mode = "sweep"
            self.input_enabled = False
            self.sweep_state = "running"
            self.safety_state = "sweep_running"
            self.safety_message = "Sweep running"
            self._stop_requested.clear()

        sweep_id = str(uuid.uuid4())
        started_at = utc_timestamp()
        points: List[Dict[str, Any]] = []
        quality = "valid"
        reason = None
        previous = None
        try:
            with self.driver.operation():
                self.driver.input_off()
                self.driver.get_mode()
                self.driver.input_state()
                self.driver.set_mode_cr()
                self.driver.set_resistance(values[0])
                open_measurement = self.driver.measure_all()
                self._validate_measurement(open_measurement, envelope)
                self.driver.input_on()
                with self._lock:
                    self.input_enabled = True
                for value in values:
                    if self._stop_requested.is_set():
                        raise OperationStopped("Sweep stopped by user")
                    if previous is not None:
                        self._validate_measurement(previous, envelope)
                    self._validate_resistance(value, envelope=envelope)
                    self.driver.set_resistance(value)
                    if settle_s:
                        if self._stop_requested.wait(settle_s):
                            raise OperationStopped("Sweep stopped by user")
                    measurement = self.driver.measure_all()
                    point = {
                        "timestamp": utc_timestamp(),
                        "resistance_setpoint_ohm": value,
                        **measurement,
                    }
                    points.append(point)
                    self._validate_measurement(measurement, envelope)
                    previous = measurement
        except SafetyViolation as exc:
            quality = "safety_abort"
            reason = str(exc)
        except OperationStopped as exc:
            quality = "incomplete"
            reason = str(exc)
        except Exception as exc:
            quality = "instrument_error"
            reason = str(exc)
        finally:
            try:
                self.driver.input_off()
            except Exception as exc:
                if reason is None:
                    quality = "instrument_error"
                    reason = f"Could not confirm input OFF: {exc}"

        completed_at = utc_timestamp()
        irradiance = self._irradiance_statistics(started_at, completed_at)
        if quality == "valid" and not points:
            quality = "incomplete"
            reason = "Sweep produced no measurement points"
        if quality == "valid" and irradiance["count"] == 0:
            quality = "incomplete"
            reason = "No SPN1 irradiance samples were captured during sweep"
        if quality == "valid" and self._irradiance_unstable(irradiance):
            quality = "unstable_irradiance"
            reason = "Irradiance changed beyond configured quality thresholds"

        mpp_point = max(points, key=lambda point: point["voltage_v"] * point["current_a"]) if points else None
        result = {
            "sweep_id": sweep_id,
            "panel_uid": self.panel_uid,
            "load_uid": self.uid,
            "load_type": self.load_type,
            "active_mode": "sweep",
            "started_at": started_at,
            "completed_at": completed_at,
            "quality": quality,
            "reason": reason,
            "points": points,
            "vmpp_v": mpp_point.get("voltage_v") if mpp_point else None,
            "impp_a": mpp_point.get("current_a") if mpp_point else None,
            "pmpp_w": (mpp_point["voltage_v"] * mpp_point["current_a"]) if mpp_point else None,
            "rmpp_ohm": mpp_point.get("load_resistance_ohm") if mpp_point else None,
            "irradiance": irradiance,
        }
        with self._lock:
            self.last_sweep = result
            self.active_mode = None
            self.input_enabled = False
            self.sweep_state = "completed" if quality in {"valid", "unstable_irradiance"} else "aborted"
            if quality == "safety_abort":
                self._latch_fault("safety_fault", reason or "Sweep safety abort")
            elif quality == "instrument_error":
                self._latch_fault("instrument_error", reason or "Sweep instrument error")
            else:
                self.safety_state = "ready"
                self.safety_message = reason or "Sweep completed"
        if self.sweep_result_sink:
            self.sweep_result_sink(result)
        return result

    def _validate_resistance(self, value: float, mode: Optional[str] = None, envelope: Optional[Dict[str, float]] = None) -> None:
        limits = envelope or self.safety_envelope(mode or self.selected_mode)
        if value < limits["min_load_resistance_ohm"] or value > limits["max_load_resistance_ohm"]:
            raise SafetyViolation(
                f"Resistance {value:g} ohm is outside safe range "
                f"{limits['min_load_resistance_ohm']:.2f}-{limits['max_load_resistance_ohm']:.2f} ohm"
            )

    def _validate_measurement(self, measurement: Dict[str, Any], envelope: Dict[str, float]) -> None:
        for field, limit_name in (
            ("voltage_v", "max_voltage_v"),
            ("current_a", "max_current_a"),
            ("power_w", "max_power_w"),
        ):
            value = numeric(measurement.get(field))
            if value is None:
                raise SafetyViolation(f"Missing or invalid runtime measurement: {field}")
            if value >= envelope[limit_name]:
                raise SafetyViolation(f"{field} reached safety limit: {value:g} >= {envelope[limit_name]:g}")

    def _safe_off(self) -> None:
        try:
            self.driver.input_off()
        finally:
            self.input_enabled = False
            self.active_mode = None

    def _latch_fault(self, state: str, message: str) -> None:
        self.safety_state = state
        self.safety_message = message
        self.input_enabled = False
        self.active_mode = None

    def _add_provenance(self, reading: Dict[str, Any]) -> None:
        extended = reading.setdefault("extended", {})
        extended.update(
            panel_uid=self.panel_uid,
            load_uid=self.uid,
            load_type=self.load_type,
            active_mode=self.active_mode,
            resistance_setpoint_ohm=self.selected_resistance_ohm,
            safety_state=self.safety_state,
        )

    def _fault_reading(self, reading: Dict[str, Any], reason: str) -> Dict[str, Any]:
        reading["status"] = "error"
        reading["message"] = reason
        reading["data"] = {key: None for key in ("voltage_v", "current_a", "power_w", "load_resistance_ohm")}
        reading.setdefault("extended", {})["error"] = reason
        return reading

    def _irradiance_statistics(self, start: str, end: str) -> Dict[str, Any]:
        values = [float(value) for value in self.irradiance_provider(start, end) if numeric(value) is not None]
        return {
            "count": len(values),
            "mean_w_m2": statistics.fmean(values) if values else None,
            "min_w_m2": min(values) if values else None,
            "max_w_m2": max(values) if values else None,
            "std_w_m2": statistics.pstdev(values) if len(values) > 1 else (0.0 if values else None),
        }

    def _irradiance_unstable(self, stats: Dict[str, Any]) -> bool:
        config = self.config.get("irradiance_quality") or {}
        max_std = numeric(config.get("max_std_w_m2"))
        max_relative_range = numeric(config.get("max_relative_range"))
        if max_std is not None and stats["std_w_m2"] is not None and stats["std_w_m2"] > max_std:
            return True
        mean = stats.get("mean_w_m2")
        if max_relative_range is not None and mean and mean > 0:
            relative_range = (stats["max_w_m2"] - stats["min_w_m2"]) / mean
            return relative_range > max_relative_range
        return False

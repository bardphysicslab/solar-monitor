from __future__ import annotations

import math
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Dict, Iterable, List, Optional, Protocol


SUPPORTED_MODES = {"fixed_resistance", "sweep"}
REQUIRED_ELECTRICAL_LIMITS = ("max_voltage_v", "max_current_a", "max_power_w")
ET54_HARDWARE_MIN_RESISTANCE_OHM = 0.05
ET54_HARDWARE_MAX_RESISTANCE_OHM = 4500.0


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


def nominal_rmpp_ohm(panel_spec: Dict[str, Any]) -> Optional[float]:
    voltage = numeric((panel_spec or {}).get("vmp_v"))
    current = numeric((panel_spec or {}).get("imp_a"))
    if voltage is None or voltage <= 0 or current is None or current <= 0:
        return None
    return voltage / current


def automatic_sweep_values(nominal_ohm: float, minimum_ohm: float, maximum_ohm: float, point_count: int = 12) -> List[float]:
    nominal = numeric(nominal_ohm)
    minimum = numeric(minimum_ohm)
    maximum = numeric(maximum_ohm)
    if nominal is None or minimum is None or maximum is None or minimum <= 0 or maximum <= minimum:
        raise SafetyConfigurationError("automatic sweep requires a valid nominal RMPP and safe resistance range")
    if not minimum < nominal < maximum:
        raise SafetyConfigurationError(
            f"safe resistance range {minimum:.3g}-{maximum:.3g} ohm does not bracket nominal RMPP {nominal:.3g} ohm"
        )
    if point_count < 5:
        raise SafetyConfigurationError("automatic sweep requires at least five points")
    ratio = (maximum / minimum) ** (1.0 / (point_count - 2))
    generated = [maximum / (ratio ** index) for index in range(point_count - 1)] + [minimum, nominal]
    return sorted({round(value, 6) for value in generated}, reverse=True)


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
        ET54_HARDWARE_MAX_RESISTANCE_OHM,
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
        ET54_HARDWARE_MIN_RESISTANCE_OHM,
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
        monotonic_fn: Callable[[], float] = time.monotonic,
        wait_fn: Optional[Callable[[float], bool]] = None,
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
        self.monotonic_fn = monotonic_fn
        self._lock = threading.RLock()
        self.selected_mode = load_config.get("default_selected_mode", "fixed_resistance")
        self.panel_spec = ((panel_config or {}).get("config") or {}).get("panel_spec") or {}
        self.nominal_rmpp_ohm = nominal_rmpp_ohm(self.panel_spec)
        self.measured_rmpp_ohm = None
        configured_resistance = numeric(
            (load_config.get("cr") or {}).get("default_resistance_ohm", load_config.get("resistance_ohm"))
        )
        self.selected_resistance_ohm = self.nominal_rmpp_ohm if self.nominal_rmpp_ohm is not None else configured_resistance
        self.resistance_source = "datasheet" if self.nominal_rmpp_ohm is not None else ("manual" if configured_resistance else None)
        self.applied_resistance_ohm = None
        self.active_mode = None
        self.input_enabled = False
        self.safety_state = "disabled"
        self.safety_message = "Load disabled"
        self.sweep_state = "idle"
        self.last_sweep = None
        self.last_successful_sweep = None
        self.last_measurement = None
        self._stop_requested = threading.Event()
        self.wait_fn = wait_fn or self._stop_requested.wait
        self.sweep_run_mode = "single_shot"
        self.sweep_run_active = False
        self._sweep_durations: List[float] = []
        self.overrun_count = 0
        self.skipped_boundary_count = 0
        self._resume_fixed_after_stop = False
        self._refresh_readiness()
        if self.selected_resistance_ohm is not None:
            try:
                self._validate_resistance(self.selected_resistance_ohm, mode="fixed_resistance")
            except LoadControlError:
                self.selected_resistance_ohm = None
                self.resistance_source = None

    def _refresh_readiness(self) -> None:
        try:
            self.safety_envelope()
        except SafetyConfigurationError as exc:
            self.safety_state = "unavailable"
            self.safety_message = str(exc)
        else:
            if self.safety_state not in {"active", "safety_fault", "instrument_error", "disconnected_unconfirmed", "sweep_running"}:
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
            try:
                _sweep_envelope, sweep_values, sweep_settle_s = self.validate_sweep_configuration()
                sweep_config_valid = True
                sweep_config_error = None
            except LoadControlError as exc:
                sweep_values = []
                sweep_settle_s = numeric((self.config.get("sweep") or {}).get("settle_s"))
                sweep_config_valid = False
                sweep_config_error = str(exc)
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
                "transport_state": getattr(self.driver, "transport_state", "connected"),
                "transport_message": getattr(self.driver, "transport_message", None),
                "resistance_setpoint_ohm": self.selected_resistance_ohm,
                "nominal_rmpp_ohm": self.nominal_rmpp_ohm,
                "measured_rmpp_ohm": self.measured_rmpp_ohm,
                "active_resistance_ohm": self.applied_resistance_ohm if self.input_enabled else None,
                "resistance_source": self.resistance_source,
                "applied_resistance_ohm": self.applied_resistance_ohm,
                "sweep_state": self.sweep_state,
                "sweep_run_mode": self.sweep_run_mode,
                "sweep_run_active": self.sweep_run_active,
                "sweep_timing_stats": self.sweep_timing_stats(),
                "last_measurement": self.last_measurement,
                "last_sweep": self.last_sweep,
                "last_successful_sweep": self.last_successful_sweep,
                "safety_config_complete": config_complete,
                "safety_config_error": config_error,
                "effective_limits": envelope,
                "sweep_config": {
                    "resistance_values_ohm": sweep_values,
                    "point_count": len(sweep_values),
                    "min_resistance_ohm": min(sweep_values) if sweep_values else None,
                    "max_resistance_ohm": max(sweep_values) if sweep_values else None,
                    "settle_s": sweep_settle_s,
                    "interval_s": numeric((self.config.get("sweep") or {}).get("interval_s")),
                    "valid": sweep_config_valid,
                    "error": sweep_config_error,
                },
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
                self.resistance_source = "manual"
            self._refresh_readiness()
            return self.state()

    def select_sweep_run_mode(self, run_mode: str) -> Dict[str, Any]:
        if run_mode not in {"single_shot", "continuous"}:
            raise LoadControlError(f"Unsupported sweep run mode: {run_mode}")
        with self._lock:
            if self.sweep_run_active or self.sweep_state == "running" or self.active_mode is not None:
                raise LoadControlError("Cannot change sweep run mode while a load operation is active")
            self.sweep_run_mode = run_mode
            return self.state()

    def sweep_timing_stats(self) -> Dict[str, Any]:
        durations = list(self._sweep_durations)
        if not durations:
            return {
                "completed_sweep_count": 0,
                "last_duration_s": None,
                "min_duration_s": None,
                "max_duration_s": None,
                "mean_duration_s": None,
                "median_duration_s": None,
                "p95_duration_s": None,
                "overrun_count": self.overrun_count,
                "skipped_boundary_count": self.skipped_boundary_count,
            }
        ordered = sorted(durations)
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return {
            "completed_sweep_count": len(durations),
            "last_duration_s": durations[-1],
            "min_duration_s": ordered[0],
            "max_duration_s": ordered[-1],
            "mean_duration_s": statistics.fmean(durations),
            "median_duration_s": statistics.median(durations),
            "p95_duration_s": ordered[p95_index],
            "overrun_count": self.overrun_count,
            "skipped_boundary_count": self.skipped_boundary_count,
        }

    def select_resistance_step(self, step_ohm: float, direction: int) -> Dict[str, Any]:
        with self._lock:
            if self.selected_resistance_ohm is None:
                raise LoadControlError("No safe automatic resistance is available; enter a validated manual value")
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
            if self.active_mode is not None or self.safety_state in {"safety_fault", "instrument_error", "disconnected_unconfirmed"}:
                raise LoadControlError("Load must be disabled and fault-cleared before enabling")
            envelope = self.safety_envelope("fixed_resistance")
            self._validate_resistance(self.selected_resistance_ohm, envelope=envelope)
            try:
                with self.driver.operation():
                    self._confirm_driver_off()
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
            self.applied_resistance_ohm = self.selected_resistance_ohm
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
            self.applied_resistance_ohm = self.selected_resistance_ohm
            return self.state()

    def disable(self, clear_fault: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._stop_requested.set()
            was_sweep_run_active = self.sweep_run_active
            self.sweep_run_active = False
            if self.input_enabled or self.active_mode is not None or self.sweep_state == "running" or was_sweep_run_active:
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

    def request_sweep_stop(self, resume_fixed: bool = True) -> Dict[str, Any]:
        with self._lock:
            if not self.sweep_run_active:
                return self.disable()
            self._resume_fixed_after_stop = bool(resume_fixed and self.sweep_run_mode == "continuous")
            self._stop_requested.set()
            self.safety_message = "Stopping sweep safely"
            return self.state()

    def _enable_fixed_target(self, resistance_ohm: Any, source: str) -> Dict[str, Any]:
        value = numeric(resistance_ohm)
        if value is None:
            raise SafetyConfigurationError("No valid resistance is available for Fixed Resistance")
        self._validate_resistance(value, mode="fixed_resistance")
        with self._lock:
            self.selected_mode = "fixed_resistance"
            self.selected_resistance_ohm = value
            self.resistance_source = source
        return self.enable_fixed()

    def poll_reading(self) -> Dict[str, Any]:
        with self._lock:
            if self.sweep_state == "running":
                raise LoadControlError("Ordinary polling is suspended during sweep")
            reading = self.driver.get_reading()
            if reading.get("status") == "ok":
                measurement = reading.get("data") or {}
                self.last_measurement = measurement
                driver_confirmed_off = getattr(self.driver, "safe_off_confirmed", False)
                if self.safety_state == "disconnected_unconfirmed" and driver_confirmed_off:
                    self.input_enabled = False
                    self.active_mode = None
                    self.safety_state = "ready"
                    self.safety_message = "ET54 reconnected; load confirmed OFF"
                recovery_requires_off = (
                    self.safety_state == "disconnected_unconfirmed"
                    or getattr(self.driver, "requires_safe_off_confirmation", False)
                )
                if recovery_requires_off:
                    try:
                        self._safe_off()
                    except Exception as exc:
                        reading = self._fault_reading(reading, f"ET54 reconnected but OFF could not be confirmed: {exc}")
                    else:
                        self.safety_state = "ready"
                        self.safety_message = "ET54 reconnected; load confirmed OFF"
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
                user_message = (
                    getattr(self.driver, "transport_message", None)
                    if reading.get("status") == "node_unavailable"
                    else None
                ) or error
                if self.active_mode is not None or self.input_enabled:
                    self._safe_off()
                    self._latch_fault("instrument_error", user_message)
                elif self.safety_state not in {"safety_fault", "instrument_error"}:
                    self.safety_state = "unavailable"
                    self.safety_message = user_message
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
            envelope = self.safety_envelope("sweep")
            values = automatic_sweep_values(
                self.nominal_rmpp_ohm,
                envelope["min_load_resistance_ohm"],
                envelope["max_load_resistance_ohm"],
                int(sweep_config.get("point_count", 12)),
            )
        if values != sorted(values, reverse=True):
            raise SafetyConfigurationError("sweep resistance values must be high-to-low")
        for value in values:
            self._validate_resistance(value, envelope=envelope)
        settle_s = numeric(sweep_config.get("settle_s"))
        if settle_s is None or settle_s < 0:
            raise SafetyConfigurationError("sweep.settle_s must be a non-negative number")
        return envelope, values, settle_s

    def run_sweep(
        self,
        scheduled_start_at: Optional[str] = None,
        scheduled_monotonic: Optional[float] = None,
        cadence_s: Optional[float] = None,
        preserve_stop_request: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            if self.active_mode is not None or self.safety_state in {"safety_fault", "instrument_error", "disconnected_unconfirmed"}:
                raise LoadControlError("Load must be disabled and fault-cleared before sweep")
            envelope, values, settle_s = self.validate_sweep_configuration()
            self.selected_mode = "sweep"
            self.active_mode = "sweep"
            self.input_enabled = False
            self.sweep_state = "running"
            self.safety_state = "sweep_running"
            self.safety_message = "Sweep running"
            if not preserve_stop_request:
                self._stop_requested.clear()

        sweep_id = str(uuid.uuid4())
        actual_started_at = utc_timestamp()
        started_monotonic = self.monotonic_fn()
        scheduled_monotonic = started_monotonic if scheduled_monotonic is None else scheduled_monotonic
        scheduled_start_at = scheduled_start_at or actual_started_at
        points: List[Dict[str, Any]] = []
        electrical_status = "complete"
        reason = None
        off_confirmed = False
        previous = None
        try:
            with self.driver.operation():
                self._confirm_driver_off()
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
                        if self.wait_fn(settle_s):
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
            electrical_status = "safety_abort"
            reason = str(exc)
        except OperationStopped as exc:
            electrical_status = "incomplete"
            reason = str(exc)
        except Exception as exc:
            electrical_status = "instrument_error"
            reason = str(exc)
        finally:
            try:
                self._confirm_driver_off()
                off_confirmed = True
            except Exception as exc:
                electrical_status = "instrument_error"
                reason = f"Could not confirm input OFF: {exc}"

        completed_at = utc_timestamp()
        completed_monotonic = self.monotonic_fn()
        duration_s = max(0.0, completed_monotonic - started_monotonic)
        irradiance = self._irradiance_statistics(actual_started_at, completed_at)
        if electrical_status == "complete" and len(points) != len(values):
            electrical_status = "incomplete"
            reason = f"Sweep completed {len(points)}/{len(values)} electrical points"
        if irradiance["count"] == 0:
            irradiance_status = "unavailable"
        elif self._irradiance_unstable(irradiance):
            irradiance_status = "unstable"
        else:
            irradiance_status = "valid"
        if electrical_status == "complete":
            quality = {
                "valid": "valid",
                "unstable": "unstable_irradiance",
                "unavailable": "irradiance_unavailable",
            }[irradiance_status]
        else:
            quality = electrical_status
        if electrical_status in {"incomplete", "safety_abort", "instrument_error"}:
            timing_status = electrical_status
        elif cadence_s is not None and completed_monotonic > scheduled_monotonic + cadence_s:
            timing_status = "overrun"
        else:
            timing_status = "on_time"

        mpp_point = max(points, key=lambda point: point["voltage_v"] * point["current_a"]) if points else None
        result = {
            "sweep_id": sweep_id,
            "panel_uid": self.panel_uid,
            "load_uid": self.uid,
            "load_type": self.load_type,
            "active_mode": "sweep",
            "scheduled_start_at": scheduled_start_at,
            "actual_started_at": actual_started_at,
            "started_at": actual_started_at,
            "completed_at": completed_at,
            "duration_s": duration_s,
            "timing_status": timing_status,
            "electrical_status": electrical_status,
            "irradiance_status": irradiance_status,
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
            if electrical_status == "complete":
                self.last_successful_sweep = result
                self.measured_rmpp_ohm = result.get("rmpp_ohm")
                self._sweep_durations.append(duration_s)
                if timing_status == "overrun":
                    self.overrun_count += 1
            self.active_mode = None
            self.input_enabled = not off_confirmed
            self.sweep_state = "completed" if electrical_status == "complete" else "aborted"
            if not off_confirmed:
                self.safety_state = "disconnected_unconfirmed"
                self.safety_message = "ET54 USB disconnected; load OFF could not be confirmed"
            elif electrical_status == "safety_abort":
                self._latch_fault("safety_fault", reason or "Sweep safety abort")
            elif electrical_status == "instrument_error":
                self._latch_fault("instrument_error", reason or "Sweep instrument error")
            else:
                self.safety_state = "ready"
                self.safety_message = reason or "Electrical sweep completed"
        if self.sweep_result_sink:
            self.sweep_result_sink(result)
        return result

    def run_sweep_sequence(self, run_mode: Optional[str] = None) -> List[Dict[str, Any]]:
        selected_run_mode = run_mode or self.sweep_run_mode
        if selected_run_mode not in {"single_shot", "continuous"}:
            raise LoadControlError(f"Unsupported sweep run mode: {selected_run_mode}")
        interval_s = numeric((self.config.get("sweep") or {}).get("interval_s"))
        if interval_s is None or interval_s <= 0:
            raise SafetyConfigurationError("sweep.interval_s must be a positive number")
        self.validate_sweep_configuration()
        with self._lock:
            if self.sweep_run_active or self.active_mode is not None:
                raise LoadControlError("Sweep run already active")
            self.sweep_run_mode = selected_run_mode
            self.sweep_run_active = True
            self._stop_requested.clear()

        results: List[Dict[str, Any]] = []
        cadence_origin = self.monotonic_fn()
        cadence_origin_wall = datetime.now(timezone.utc)
        boundary_index = 0
        resume_fixed_after_stop = False
        try:
            while not self._stop_requested.is_set():
                scheduled_monotonic = cadence_origin + boundary_index * interval_s
                wait_s = max(0.0, scheduled_monotonic - self.monotonic_fn())
                if wait_s and self.wait_fn(wait_s):
                    break
                result = self.run_sweep(
                    scheduled_start_at=(cadence_origin_wall + timedelta(seconds=boundary_index * interval_s)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    scheduled_monotonic=scheduled_monotonic,
                    cadence_s=interval_s,
                    preserve_stop_request=True,
                )
                results.append(result)
                if selected_run_mode == "single_shot":
                    break
                if result["electrical_status"] in {"safety_abort", "instrument_error"}:
                    break
                if self._stop_requested.is_set():
                    break

                next_index = boundary_index + 1
                now = self.monotonic_fn()
                while cadence_origin + next_index * interval_s < now:
                    next_index += 1
                skipped = max(0, next_index - boundary_index - 1)
                with self._lock:
                    self.skipped_boundary_count += skipped
                boundary_index = next_index
        finally:
            try:
                self._confirm_driver_off()
            except Exception as exc:
                with self._lock:
                    self.safety_state = "disconnected_unconfirmed"
                    self.safety_message = "ET54 USB disconnected; load OFF could not be confirmed"
                    self.input_enabled = True
            finally:
                with self._lock:
                    if self.safety_state != "disconnected_unconfirmed":
                        self.input_enabled = False
                    self.active_mode = None
                    self.sweep_run_active = False
                    resume_fixed_after_stop = self._resume_fixed_after_stop
                    self._resume_fixed_after_stop = False
        if selected_run_mode == "single_shot" and results and results[-1]["electrical_status"] == "complete":
            try:
                self._enable_fixed_target(results[-1].get("rmpp_ohm"), "measured_sweep")
            except Exception as exc:
                with self._lock:
                    self.safety_state = "instrument_error" if not isinstance(exc, SafetyViolation) else "safety_fault"
                    self.safety_message = f"Sweep completed but Fixed Resistance could not resume: {exc}"
                    self.input_enabled = False
                    self.active_mode = None
        elif selected_run_mode == "continuous" and resume_fixed_after_stop:
            target = self.measured_rmpp_ohm if self.measured_rmpp_ohm is not None else self.nominal_rmpp_ohm
            source = "measured_sweep" if self.measured_rmpp_ohm is not None else "datasheet"
            try:
                self._enable_fixed_target(target, source)
                with self._lock:
                    self.safety_message = f"Fixed Resistance resumed using {source.replace('_', ' ')} RMPP"
            except Exception as exc:
                with self._lock:
                    self.safety_state = "instrument_error" if not isinstance(exc, SafetyViolation) else "safety_fault"
                    self.safety_message = f"Continuous sweep stopped; Fixed Resistance remains OFF: {exc}"
                    self.input_enabled = False
                    self.active_mode = None
        return results

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
            self._confirm_driver_off()
        except Exception as exc:
            self.safety_state = "disconnected_unconfirmed"
            self.safety_message = "ET54 USB disconnected; load OFF could not be confirmed"
            self.active_mode = None
            self.input_enabled = True
            raise exc
        else:
            self.input_enabled = False
            self.active_mode = None

    def _confirm_driver_off(self) -> None:
        ensure_safe_off = getattr(self.driver, "ensure_safe_off", None)
        if ensure_safe_off is not None:
            ensure_safe_off()
        else:
            self.driver.input_off()

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

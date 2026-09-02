from contextlib import contextmanager
import unittest

from raspi.load_control import (
    ElectronicLoadController,
    LoadControlError,
    SafetyConfigurationError,
    SafetyViolation,
    automatic_sweep_values,
    effective_safety_envelope,
    nominal_rmpp_ohm,
    resistance_step,
)


def panel_config(with_limits=True):
    config = {}
    if with_limits:
        config["limits"] = {
            "absolute": {"max_voltage_v": 30, "max_current_a": 5, "max_power_w": 100},
            "operating": {"max_voltage_v": 25, "max_current_a": 2, "max_power_w": 50},
        }
    return {"driver": "wifi_node", "uid": "panel-001", "config": config}


def load_config(with_limits=True):
    config = {
        "panel_uid": "panel-001",
        "load_type": "electronic_load",
        "default_selected_mode": "fixed_resistance",
        "cr": {
            "default_resistance_ohm": 1370,
            "min_load_resistance_ohm": 800,
            "max_load_resistance_ohm": 4500,
        },
        "sweep": {
            "interval_s": 10,
            "settle_s": 0,
            "min_load_resistance_ohm": 800,
            "max_load_resistance_ohm": 4500,
            "resistance_values_ohm": [1200, 1000, 800],
        },
        "irradiance_quality": {"max_std_w_m2": 20, "max_relative_range": 0.1},
    }
    if with_limits:
        config["limits"] = {
            "absolute": {
                "max_voltage_v": 120,
                "max_current_a": 20,
                "max_power_w": 200,
                "min_load_resistance_ohm": 0.05,
                "max_load_resistance_ohm": 4500,
            },
            "operating": {
                "max_voltage_v": 60,
                "max_current_a": 5,
                "max_power_w": 100,
                "min_load_resistance_ohm": 10,
                "max_load_resistance_ohm": 4500,
            },
        }
    return config


class FakeLoad:
    def __init__(self, measurements=None):
        self.uid = "load-001"
        self.measurements = list(measurements or [])
        self.commands = []
        self.input_enabled = False
        self.raise_on_measure = None
        self.measure_count = 0
        self.on_measure = None
        self.reading_status = "ok"

    @contextmanager
    def operation(self):
        self.commands.append("operation:start")
        try:
            yield self
        finally:
            self.commands.append("operation:end")

    def input_off(self):
        self.commands.append("off")
        self.input_enabled = False

    def input_on(self):
        self.commands.append("on")
        self.input_enabled = True

    def get_mode(self):
        self.commands.append("get_mode")
        return "CR"

    def input_state(self):
        self.commands.append("input_state")
        return self.input_enabled

    def set_mode_cr(self):
        self.commands.append("mode:CR")

    def set_resistance(self, value):
        self.commands.append(f"resistance:{float(value):g}")

    def measure_all(self):
        self.commands.append("measure")
        self.measure_count += 1
        if self.on_measure:
            self.on_measure(self.measure_count)
        if self.raise_on_measure:
            raise self.raise_on_measure
        return self.measurements.pop(0)

    def get_reading(self):
        measurement = self.measure_all()
        return {
            "uid": self.uid,
            "timestamp": "2026-09-01T12:00:00Z",
            "status": self.reading_status,
            "message": "Fresh valid reading" if self.reading_status == "ok" else "serial disconnected",
            "data": measurement if self.reading_status == "ok" else {key: None for key in measurement},
            "extended": {} if self.reading_status == "ok" else {"error": "serial disconnected"},
            "raw": "",
        }


def measurement(voltage=16.2, current=0.02, power=0.324, resistance=800):
    return {
        "voltage_v": voltage,
        "current_a": current,
        "power_w": power,
        "load_resistance_ohm": resistance,
    }


def controller(driver=None, panel=None, config=None, irradiance=None, sink=None, monotonic_fn=None, wait_fn=None):
    kwargs = {}
    if monotonic_fn is not None:
        kwargs["monotonic_fn"] = monotonic_fn
    if wait_fn is not None:
        kwargs["wait_fn"] = wait_fn
    return ElectronicLoadController(
        driver or FakeLoad([measurement()]),
        config or load_config(),
        panel if panel is not None else panel_config(),
        irradiance_provider=lambda _start, _end: list(irradiance if irradiance is not None else [500, 502, 498]),
        sweep_result_sink=sink,
        **kwargs,
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def wait(self, seconds):
        self.waits.append(seconds)
        self.advance(seconds)
        return False


class LoadControlTest(unittest.TestCase):
    def test_nominal_rmpp_requires_valid_vmp_and_imp(self):
        self.assertAlmostEqual(nominal_rmpp_ohm({"vmp_v": 7.28, "imp_a": 0.330}), 22.060606, places=5)
        self.assertIsNone(nominal_rmpp_ohm({"vmp_v": 7.28, "imp_a": None}))
        self.assertIsNone(nominal_rmpp_ohm({"vmp_v": 0, "imp_a": 0.33}))

    def test_automatic_sweep_is_deterministic_descending_and_contains_nominal(self):
        first = automatic_sweep_values(1000, 100, 4500, point_count=10)
        second = automatic_sweep_values(1000, 100, 4500, point_count=10)
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first, reverse=True))
        self.assertIn(1000, first)
        self.assertEqual(first[0], 4500)
        self.assertEqual(first[-1], 100)

    def test_automatic_sweep_rejects_range_that_cannot_bracket_nominal(self):
        with self.assertRaises(SafetyConfigurationError):
            automatic_sweep_values(22.1, 82.5, 4500)

    def test_safe_minimum_is_derived_without_100_ohm_fallback(self):
        panel_limits = {
            "absolute": {"max_voltage_v": 10, "max_current_a": 1, "max_power_w": 10},
            "operating": {"max_voltage_v": 10, "max_current_a": 0.5, "max_power_w": 5},
        }
        load_limits = {
            "absolute": {"max_voltage_v": 120, "max_current_a": 20, "max_power_w": 200, "max_load_resistance_ohm": 4500},
            "operating": {"max_voltage_v": 60, "max_current_a": 5, "max_power_w": 100, "max_load_resistance_ohm": 4500},
        }
        envelope = effective_safety_envelope(panel_limits, load_limits, {"max_load_resistance_ohm": 4500})
        self.assertEqual(envelope["min_load_resistance_ohm"], 20.0)
        self.assertNotEqual(envelope["min_load_resistance_ohm"], 100.0)

    def test_startup_preselects_safe_datasheet_rmpp_without_energizing(self):
        panel = panel_config()
        panel["config"]["panel_spec"] = {"vmp_v": 20, "imp_a": 0.02}
        load = controller(panel=panel)
        state = load.state()
        self.assertEqual(state["nominal_rmpp_ohm"], 1000)
        self.assertEqual(state["resistance_setpoint_ohm"], 1000)
        self.assertEqual(state["resistance_source"], "datasheet")
        self.assertFalse(state["input_enabled"])
        self.assertNotIn("on", load.driver.commands)

    def test_effective_envelope_uses_most_restrictive_limits(self):
        envelope = effective_safety_envelope(
            panel_config()["config"]["limits"],
            load_config()["limits"],
            load_config()["cr"],
        )
        self.assertEqual(envelope["max_voltage_v"], 25)
        self.assertEqual(envelope["max_current_a"], 2)
        self.assertEqual(envelope["max_power_w"], 50)
        self.assertEqual(envelope["min_load_resistance_ohm"], 800)
        self.assertEqual(envelope["max_load_resistance_ohm"], 4500)

    def test_missing_panel_or_load_limits_fail_closed_for_both_modes(self):
        missing_panel = controller(panel=panel_config(with_limits=False))
        for mode in ("fixed_resistance", "sweep"):
            with self.subTest(owner="panel", mode=mode):
                with self.assertRaises(SafetyConfigurationError):
                    missing_panel.safety_envelope(mode)

        missing_load = controller(config=load_config(with_limits=False))
        for mode in ("fixed_resistance", "sweep"):
            with self.subTest(owner="load", mode=mode):
                with self.assertRaises(SafetyConfigurationError):
                    missing_load.safety_envelope(mode)

    def test_digit_steps_and_clamping(self):
        self.assertEqual(resistance_step(1370, 10, 1, 800, 4500), 1380)
        self.assertEqual(resistance_step(1370, 100, 1, 800, 4500), 1470)
        self.assertEqual(resistance_step(1370, 1000, 1, 800, 4500), 2370)
        self.assertEqual(resistance_step(4500, 10, 1, 800, 4500), 4500)
        self.assertEqual(resistance_step(800, 10, -1, 800, 4500), 800)

    def test_selection_and_stepping_do_not_send_instrument_commands(self):
        driver = FakeLoad()
        load = controller(driver=driver)
        load.select_mode("fixed_resistance", 1370)
        load.select_resistance_step(10, 1)
        self.assertEqual(load.state()["resistance_setpoint_ohm"], 1380)
        self.assertEqual(driver.commands, [])

    def test_state_exposes_valid_sweep_summary_without_touching_instrument(self):
        driver = FakeLoad()
        load = controller(driver=driver)
        state = load.state()
        self.assertEqual(state["sweep_config"]["resistance_values_ohm"], [1200.0, 1000.0, 800.0])
        self.assertEqual(state["sweep_config"]["point_count"], 3)
        self.assertEqual(state["sweep_config"]["max_resistance_ohm"], 1200.0)
        self.assertEqual(state["sweep_config"]["min_resistance_ohm"], 800.0)
        self.assertTrue(state["sweep_config"]["valid"])
        self.assertEqual(driver.commands, [])

    def test_state_exposes_invalid_sweep_reason_without_activation(self):
        config = load_config()
        config["sweep"]["resistance_values_ohm"] = ["REPLACE_WITH_SAFE_HIGH_RESISTANCE"]
        driver = FakeLoad()
        state = controller(driver=driver, config=config).state()
        self.assertFalse(state["sweep_config"]["valid"])
        self.assertEqual(state["sweep_config"]["error"], "sweep resistance values must be numeric")
        self.assertFalse(state["input_enabled"])
        self.assertIsNone(state["active_mode"])
        self.assertEqual(driver.commands, [])

    def test_enable_prepares_while_off_then_energizes_explicitly(self):
        driver = FakeLoad([measurement(current=0, power=0, resistance=1370), measurement(resistance=1370)])
        load = controller(driver=driver)
        state = load.enable_fixed()
        self.assertEqual(state["active_mode"], "fixed_resistance")
        self.assertTrue(state["input_enabled"])
        self.assertLess(driver.commands.index("off"), driver.commands.index("mode:CR"))
        self.assertLess(driver.commands.index("resistance:1370"), driver.commands.index("on"))

    def test_apply_is_explicit_and_runtime_safety_fault_turns_input_off(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1370),
            measurement(resistance=1370),
            measurement(current=2.0, power=32.4),
        ])
        load = controller(driver=driver)
        load.enable_fixed()
        load.select_resistance_step(100, 1)
        with self.assertRaises(SafetyViolation):
            load.apply_fixed()
        self.assertEqual(driver.commands[-1], "off")
        self.assertEqual(load.state()["safety_state"], "safety_fault")
        with self.assertRaises(LoadControlError):
            load.enable_fixed()

    def test_active_fixed_polling_preserves_real_current_and_power(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1370),
            measurement(current=0.009, power=0.144, resistance=1800),
            measurement(current=0.009, power=0.144, resistance=1800),
        ])
        load = controller(driver=driver)
        load.enable_fixed()
        reading = load.poll_reading()
        self.assertEqual(reading["data"]["current_a"], 0.009)
        self.assertEqual(reading["data"]["power_w"], 0.144)
        self.assertEqual(reading["data"]["load_resistance_ohm"], 1800)

    def test_voltage_current_and_power_limits_abort(self):
        cases = [
            measurement(voltage=25),
            measurement(current=2),
            measurement(power=50),
        ]
        for unsafe in cases:
            with self.subTest(unsafe=unsafe):
                driver = FakeLoad([unsafe])
                load = controller(driver=driver)
                with self.assertRaises(SafetyViolation):
                    load.enable_fixed()
                self.assertIn("off", driver.commands)
                self.assertEqual(load.state()["safety_state"], "safety_fault")

    def test_transport_exception_attempts_off_and_latches_instrument_error(self):
        driver = FakeLoad()
        driver.raise_on_measure = OSError("serial disconnected")
        load = controller(driver=driver)
        with self.assertRaises(OSError):
            load.enable_fixed()
        self.assertEqual(driver.commands[-1], "off")
        self.assertEqual(load.state()["safety_state"], "instrument_error")

    def test_normalized_transport_failure_while_active_attempts_off_and_latches(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1370),
            measurement(resistance=1370),
            measurement(resistance=1370),
        ])
        load = controller(driver=driver)
        load.enable_fixed()
        driver.reading_status = "node_unavailable"
        reading = load.poll_reading()
        self.assertEqual(reading["status"], "node_unavailable")
        self.assertEqual(driver.commands[-1], "off")
        self.assertEqual(load.state()["safety_state"], "instrument_error")
        self.assertFalse(load.state()["input_enabled"])

    def test_failed_off_is_latched_as_disconnected_and_unconfirmed(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1370),
            measurement(resistance=1370),
        ])
        load = controller(driver=driver)
        load.enable_fixed()
        driver.input_off = lambda: (_ for _ in ()).throw(OSError(6, "Device not configured"))

        with self.assertRaises(OSError):
            load.disable()

        state = load.state()
        self.assertEqual(state["safety_state"], "disconnected_unconfirmed")
        self.assertTrue(state["input_enabled"])
        self.assertIsNone(state["active_mode"])

    def test_recovery_confirms_off_without_resuming_previous_mode(self):
        driver = FakeLoad([measurement()])
        confirmed = []
        driver.ensure_safe_off = lambda: confirmed.append("off-confirmed")
        load = controller(driver=driver)
        load.safety_state = "disconnected_unconfirmed"
        load.safety_message = "ET54 USB disconnected; load OFF could not be confirmed"
        load.active_mode = None
        load.input_enabled = True
        load.sweep_run_mode = "continuous"
        load.sweep_run_active = False

        reading = load.poll_reading()

        self.assertEqual(reading["status"], "ok")
        self.assertEqual(confirmed, ["off-confirmed"])
        state = load.state()
        self.assertEqual(state["safety_state"], "ready")
        self.assertFalse(state["input_enabled"])
        self.assertIsNone(state["active_mode"])
        self.assertFalse(state["sweep_run_active"])

    def test_disconnect_during_sweep_preserves_points_and_leaves_off_unconfirmed(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1200),
            measurement(voltage=16, current=0.01, resistance=1200),
        ])
        off_calls = 0

        def fail_final_off():
            nonlocal off_calls
            off_calls += 1
            if off_calls > 1:
                raise OSError(6, "Device not configured")
            driver.input_enabled = False

        driver.input_off = fail_final_off
        driver.on_measure = lambda count: setattr(driver, "raise_on_measure", OSError(6, "Device not configured")) if count == 3 else None
        load = controller(driver=driver)

        result = load.run_sweep()

        self.assertEqual(result["electrical_status"], "instrument_error")
        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(load.state()["safety_state"], "disconnected_unconfirmed")
        self.assertTrue(load.state()["input_enabled"])

    def test_sweep_high_to_low_preserves_points_and_selects_computed_mpp(self):
        driver = FakeLoad([
            measurement(voltage=20, current=0, power=0, resistance=1200),
            measurement(voltage=20, current=0.01, power=0.1, resistance=1200),
            measurement(voltage=18, current=0.03, power=0.2, resistance=1000),
            measurement(voltage=15, current=0.02, power=0.5, resistance=800),
        ])
        saved = []
        load = controller(driver=driver, sink=saved.append)
        result = load.run_sweep()
        resistance_commands = [command for command in driver.commands if command.startswith("resistance:")]
        self.assertEqual(resistance_commands, ["resistance:1200", "resistance:1200", "resistance:1000", "resistance:800"])
        self.assertEqual(len(result["points"]), 3)
        self.assertEqual(result["quality"], "valid")
        self.assertEqual(result["vmpp_v"], 18)
        self.assertEqual(result["impp_a"], 0.03)
        self.assertAlmostEqual(result["pmpp_w"], 0.54)
        self.assertEqual(result["rmpp_ohm"], 1000)
        self.assertEqual(result["irradiance"]["mean_w_m2"], 500)
        self.assertEqual(saved, [result])
        self.assertIs(load.state()["last_successful_sweep"], result)
        self.assertEqual(driver.commands[-1], "off")

    def test_sweep_rejects_low_to_high_before_energizing(self):
        config = load_config()
        config["sweep"]["resistance_values_ohm"] = [800, 1000, 1200]
        driver = FakeLoad()
        load = controller(driver=driver, config=config)
        with self.assertRaises(SafetyConfigurationError):
            load.run_sweep()
        self.assertEqual(driver.commands, [])

    def test_sweep_safety_abort_stops_before_next_lower_point(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1200),
            measurement(resistance=1200),
            measurement(current=2.0, power=32.4, resistance=1000),
            measurement(resistance=800),
        ])
        load = controller(driver=driver)
        result = load.run_sweep()
        self.assertEqual(result["quality"], "safety_abort")
        self.assertIsNone(load.state()["last_successful_sweep"])
        self.assertEqual(len(result["points"]), 2)
        self.assertNotIn("resistance:800", driver.commands)
        self.assertEqual(driver.commands[-1], "off")

    def test_stop_request_prevents_next_lower_sweep_step(self):
        driver = FakeLoad([
            measurement(current=0, power=0, resistance=1200),
            measurement(resistance=1200),
            measurement(resistance=1000),
        ])
        load = controller(driver=driver)
        driver.on_measure = lambda count: load._stop_requested.set() if count == 2 else None
        result = load.run_sweep()
        self.assertEqual(result["quality"], "incomplete")
        self.assertIn("stopped by user", result["reason"])
        self.assertEqual(len(result["points"]), 1)
        self.assertNotIn("resistance:1000", driver.commands)
        self.assertEqual(driver.commands[-1], "off")

    def test_unstable_and_missing_irradiance_quality(self):
        unstable = controller(
            driver=FakeLoad([measurement(current=0, power=0), measurement(), measurement(), measurement()]),
            irradiance=[300, 500, 700],
        ).run_sweep()
        self.assertEqual(unstable["quality"], "unstable_irradiance")
        incomplete = controller(
            driver=FakeLoad([measurement(current=0, power=0), measurement(), measurement(), measurement()]),
            irradiance=[],
        ).run_sweep()
        self.assertEqual(incomplete["electrical_status"], "complete")
        self.assertEqual(incomplete["irradiance_status"], "unavailable")
        self.assertEqual(incomplete["quality"], "irradiance_unavailable")
        self.assertIsNotNone(incomplete["vmpp_v"])

    def test_failed_sweep_preserves_previous_successful_sweep(self):
        driver = FakeLoad([measurement(current=0, power=0), measurement(), measurement(), measurement()])
        load = controller(driver=driver)
        successful = load.run_sweep()
        driver.raise_on_measure = OSError("serial disconnected")

        failed = load.run_sweep()

        self.assertEqual(failed["electrical_status"], "instrument_error")
        self.assertIs(load.state()["last_sweep"], failed)
        self.assertIs(load.state()["last_successful_sweep"], successful)

    def test_failed_single_shot_runs_once_and_leaves_input_off(self):
        driver = FakeLoad([measurement(current=0, power=0), measurement(current=2.0, power=32.4)])
        load = controller(driver=driver)
        results = load.run_sweep_sequence("single_shot")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["electrical_status"], "safety_abort")
        self.assertFalse(load.state()["sweep_run_active"])
        self.assertFalse(driver.input_enabled)
        self.assertEqual(driver.commands[-1], "off")

    def test_successful_single_shot_resumes_fixed_at_measured_rmpp(self):
        driver = FakeLoad([
            measurement(current=0, power=0),
            measurement(voltage=16, current=0.01, resistance=1200),
            measurement(voltage=15, current=0.02, resistance=1000),
            measurement(voltage=14, current=0.01, resistance=800),
            measurement(voltage=16, current=0, power=0, resistance=1000),
            measurement(voltage=15, current=0.02, power=0.3, resistance=1000),
        ])
        load = controller(driver=driver)
        results = load.run_sweep_sequence("single_shot")
        state = load.state()
        self.assertEqual(results[0]["rmpp_ohm"], 1000)
        self.assertEqual(state["selected_mode"], "fixed_resistance")
        self.assertEqual(state["resistance_setpoint_ohm"], 1000)
        self.assertEqual(state["resistance_source"], "measured_sweep")
        self.assertTrue(state["input_enabled"])
        self.assertEqual(state["active_mode"], "fixed_resistance")

    def test_continuous_uses_fixed_boundary_when_sweep_finishes_at_9_9(self):
        clock = FakeClock()
        driver = FakeLoad([measurement(current=0, power=0), measurement(), measurement(), measurement()] * 2)
        driver.on_measure = lambda _count: clock.advance(2.475)
        saved = []
        load = controller(driver=driver, sink=saved.append, monotonic_fn=clock.monotonic, wait_fn=clock.wait)

        def stop_after_two(result):
            saved.append(result)
            if len(saved) == 2:
                load.disable()

        load.sweep_result_sink = stop_after_two
        results = load.run_sweep_sequence("continuous")
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(clock.waits[0], 0.1)
        self.assertEqual(load.state()["sweep_timing_stats"]["skipped_boundary_count"], 0)
        self.assertEqual(results[0]["timing_status"], "on_time")

    def test_continuous_successive_sweeps_replace_successful_mpp_atomically(self):
        clock = FakeClock()
        driver = FakeLoad([
            measurement(current=0, power=0),
            measurement(voltage=16, current=0.01, resistance=1200),
            measurement(voltage=15, current=0.02, resistance=1000),
            measurement(voltage=14, current=0.01, resistance=800),
            measurement(current=0, power=0),
            measurement(voltage=15, current=0.01, resistance=1200),
            measurement(voltage=14, current=0.02, resistance=1000),
            measurement(voltage=13, current=0.03, resistance=800),
        ])
        saved = []
        load = controller(driver=driver, monotonic_fn=clock.monotonic, wait_fn=clock.wait)

        def retain_and_stop(result):
            saved.append(result)
            if len(saved) == 2:
                load.disable()

        load.sweep_result_sink = retain_and_stop
        results = load.run_sweep_sequence("continuous")

        self.assertEqual(len(results), 2)
        self.assertEqual((results[0]["vmpp_v"], results[0]["impp_a"], results[0]["rmpp_ohm"]), (15, 0.02, 1000))
        self.assertEqual((results[1]["vmpp_v"], results[1]["impp_a"], results[1]["rmpp_ohm"]), (13, 0.03, 800))
        self.assertIs(load.state()["last_successful_sweep"], results[1])

    def test_intentional_continuous_stop_resumes_fixed_at_latest_measured_rmpp(self):
        driver = FakeLoad([
            measurement(current=0, power=0),
            measurement(voltage=16, current=0.01, resistance=1200),
            measurement(voltage=15, current=0.02, resistance=1000),
            measurement(voltage=14, current=0.01, resistance=800),
            measurement(voltage=16, current=0, power=0, resistance=1000),
            measurement(voltage=15, current=0.02, power=0.3, resistance=1000),
        ])
        load = controller(driver=driver)
        load.sweep_result_sink = lambda _result: load.request_sweep_stop(resume_fixed=True)

        results = load.run_sweep_sequence("continuous")

        self.assertEqual(len(results), 1)
        state = load.state()
        self.assertEqual(state["active_mode"], "fixed_resistance")
        self.assertTrue(state["input_enabled"])
        self.assertEqual(state["resistance_setpoint_ohm"], results[0]["rmpp_ohm"])
        self.assertEqual(state["resistance_source"], "measured_sweep")

    def test_intentional_continuous_stop_without_completed_sweep_uses_nominal_rmpp(self):
        panel = panel_config()
        panel["config"]["panel_spec"] = {"vmp_v": 20, "imp_a": 0.02}
        driver = FakeLoad([
            measurement(current=0, power=0),
            measurement(voltage=16, current=0.01, resistance=1200),
            measurement(voltage=16, current=0, power=0, resistance=1000),
            measurement(voltage=15, current=0.02, power=0.3, resistance=1000),
        ])
        load = controller(driver=driver, panel=panel)
        driver.on_measure = lambda count: load.request_sweep_stop(resume_fixed=True) if count == 2 else None

        results = load.run_sweep_sequence("continuous")

        self.assertEqual(results[0]["electrical_status"], "incomplete")
        state = load.state()
        self.assertEqual(state["active_mode"], "fixed_resistance")
        self.assertTrue(state["input_enabled"])
        self.assertEqual(state["resistance_setpoint_ohm"], 1000)
        self.assertEqual(state["resistance_source"], "datasheet")

    def test_continuous_overrun_is_retained_and_resumes_next_future_boundary(self):
        clock = FakeClock()
        driver = FakeLoad([measurement(current=0, power=0), measurement(), measurement(), measurement()] * 2)
        driver.on_measure = lambda _count: clock.advance(2.575)
        saved = []
        load = controller(driver=driver, monotonic_fn=clock.monotonic, wait_fn=clock.wait)

        def retain_and_stop(result):
            saved.append(result)
            if len(saved) == 2:
                load.disable()

        load.sweep_result_sink = retain_and_stop
        results = load.run_sweep_sequence("continuous")
        self.assertEqual(results, saved)
        self.assertEqual(results[0]["timing_status"], "overrun")
        self.assertAlmostEqual(results[0]["duration_s"], 10.3)
        self.assertAlmostEqual(clock.waits[0], 9.7)
        stats = load.state()["sweep_timing_stats"]
        self.assertEqual(stats["overrun_count"], 2)
        self.assertEqual(stats["skipped_boundary_count"], 1)
        operation_events = [item for item in driver.commands if item.startswith("operation:")]
        self.assertEqual(operation_events, ["operation:start", "operation:end", "operation:start", "operation:end"])
        self.assertFalse(driver.input_enabled)

    def test_safety_fault_ends_continuous_run(self):
        driver = FakeLoad([
            measurement(current=0, power=0),
            measurement(current=2.0, power=32.4),
        ])
        saved = []
        load = controller(driver=driver, sink=saved.append)
        results = load.run_sweep_sequence("continuous")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["electrical_status"], "safety_abort")
        self.assertEqual(load.state()["safety_state"], "safety_fault")
        self.assertFalse(driver.input_enabled)

    def test_instrument_fault_ends_continuous_run(self):
        driver = FakeLoad()
        driver.raise_on_measure = OSError("serial disconnected")
        load = controller(driver=driver)
        results = load.run_sweep_sequence("continuous")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["electrical_status"], "instrument_error")
        self.assertEqual(load.state()["safety_state"], "instrument_error")
        self.assertFalse(driver.input_enabled)

    def test_duration_statistics(self):
        load = controller()
        load._sweep_durations = [5.0, 6.0, 7.0, 8.0, 20.0]
        load.overrun_count = 1
        load.skipped_boundary_count = 1
        stats = load.sweep_timing_stats()
        self.assertEqual(stats["min_duration_s"], 5.0)
        self.assertEqual(stats["max_duration_s"], 20.0)
        self.assertEqual(stats["mean_duration_s"], 9.2)
        self.assertEqual(stats["median_duration_s"], 7.0)
        self.assertEqual(stats["p95_duration_s"], 20.0)


if __name__ == "__main__":
    unittest.main()

from contextlib import contextmanager
import unittest

from raspi.load_control import (
    ElectronicLoadController,
    LoadControlError,
    SafetyConfigurationError,
    SafetyViolation,
    effective_safety_envelope,
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


def controller(driver=None, panel=None, config=None, irradiance=None, sink=None):
    return ElectronicLoadController(
        driver or FakeLoad([measurement()]),
        config or load_config(),
        panel if panel is not None else panel_config(),
        irradiance_provider=lambda _start, _end: list(irradiance if irradiance is not None else [500, 502, 498]),
        sweep_result_sink=sink,
    )


class LoadControlTest(unittest.TestCase):
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
        self.assertEqual(incomplete["quality"], "incomplete")


if __name__ == "__main__":
    unittest.main()

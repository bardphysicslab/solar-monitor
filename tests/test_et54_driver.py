import unittest

from raspi.drivers.et54_driver import ET54Driver, ET54ProtocolError


class FakeSerial:
    def __init__(self, responses):
        self.responses = list(responses)
        self.writes = []
        self.is_open = True
        self.reset_calls = 0

    def write(self, payload):
        self.writes.append(payload.decode("ascii"))

    def flush(self):
        pass

    def readline(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response.encode("ascii")

    def reset_input_buffer(self):
        self.reset_calls += 1

    def close(self):
        self.is_open = False


def make_driver(responses):
    fake = FakeSerial(responses)
    driver = ET54Driver(
        uid="bb-solar-load-001",
        port="/dev/fake-et54",
        serial_factory=lambda **_kwargs: fake,
    )
    return driver, fake


class ET54DriverTest(unittest.TestCase):
    def test_parse_measure_all_and_strip_leading_r(self):
        driver, _fake = make_driver([])
        parsed = driver._parse_measure_all_response("R 0.050   5.055  0.25  100.351")
        self.assertEqual(
            parsed,
            {
                "current_a": 0.05,
                "voltage_v": 5.055,
                "power_w": 0.25,
                "load_resistance_ohm": 100.351,
            },
        )
        self.assertEqual(driver._strip_response_prefix("R100.00"), "100.00")

    def test_measure_all_public_method(self):
        driver, fake = make_driver(["R 0.050 5.055 0.25 100.351\n"])
        self.assertEqual(
            driver.measure_all(),
            {
                "current_a": 0.05,
                "voltage_v": 5.055,
                "power_w": 0.25,
                "load_resistance_ohm": 100.351,
            },
        )
        self.assertEqual(fake.writes, ["MEAS:ALL?\n"])

    def test_setter_acknowledgment_is_consumed_before_query(self):
        driver, fake = make_driver(["Rexecu success\n", "R100.00\n"])
        driver.set_resistance(100)
        self.assertEqual(driver.get_resistance(), 100.0)
        self.assertEqual(fake.writes, ["RESI:CR 100\n", "RESI:CR?\n"])
        self.assertEqual(fake.responses, [])

    def test_command_error_is_rejected(self):
        driver, _fake = make_driver(["Rcmd err\n"])
        with self.assertRaises(ET54ProtocolError):
            driver.get_resistance()

    def test_cr_mode_resistance_and_input_controls(self):
        driver, fake = make_driver(
            [
                "RCR\n",
                "Rexecu success\n",
                "Rexecu success\n",
                "RON\n",
                "Rexecu success\n",
                "ROFF\n",
            ]
        )
        self.assertEqual(driver.get_mode(), "CR")
        driver.set_mode_cr()
        driver.input_on()
        self.assertTrue(driver.input_state())
        driver.input_off()
        self.assertFalse(driver.input_state())
        self.assertIn("CH:SW OFF\n", fake.writes)

    def test_get_reading_has_bardbox_shape(self):
        driver, _fake = make_driver(["R 0.050 5.055 0.25 100.351\n"])
        reading = driver.get_reading()
        self.assertEqual(reading["uid"], "bb-solar-load-001")
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["message"], "Fresh valid reading")
        self.assertEqual(set(reading["data"]), {"voltage_v", "current_a", "power_w", "load_resistance_ohm"})
        self.assertIn("extended", reading)
        self.assertIn("raw", reading)

    def test_invalid_response_returns_null_channels(self):
        driver, _fake = make_driver(["R nonsense\n"])
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "error")
        self.assertTrue(all(value is None for value in reading["data"].values()))

    def test_transport_failure_returns_unavailable_and_resets_connection(self):
        driver, fake = make_driver([OSError("disconnected")])
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "node_unavailable")
        self.assertTrue(all(value is None for value in reading["data"].values()))
        self.assertFalse(fake.is_open)

    def test_sweep_ordering_and_result_collection(self):
        responses = [
            "RCR\n",
            "Rexecu success\n",
            "Rexecu success\n",
            "Rexecu success\n",
            "R 0.025 5.0 0.125 200.0\n",
            "Rexecu success\n",
            "R 0.05 5.0 0.25 100.0\n",
            "Rexecu success\n",
        ]
        driver, fake = make_driver(responses)
        result = driver.sweep_resistance([200, 100], settle_s=0)
        self.assertEqual([point["set_resistance_ohm"] for point in result["points"]], [200.0, 100.0])
        self.assertEqual(result["points"][1]["current_a"], 0.05)
        self.assertEqual(
            fake.writes,
            [
                "CH:MODE?\n",
                "RESI:CR 200\n",
                "CH:SW ON\n",
                "RESI:CR 200\n",
                "MEAS:ALL?\n",
                "RESI:CR 100\n",
                "MEAS:ALL?\n",
                "CH:SW OFF\n",
            ],
        )

    def test_sweep_attempts_input_off_after_exception(self):
        responses = [
            "RCR\n",
            "Rexecu success\n",
            "Rexecu success\n",
            "Rexecu success\n",
            "Rcmd err\n",
            "Rexecu success\n",
        ]
        driver, fake = make_driver(responses)
        with self.assertRaises(ET54ProtocolError):
            driver.sweep_resistance([100], settle_s=0)
        self.assertEqual(fake.writes[-1], "CH:SW OFF\n")


if __name__ == "__main__":
    unittest.main()

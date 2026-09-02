import unittest
from types import SimpleNamespace

from raspi.drivers.et54_driver import ET54Driver, ET54ProtocolError, ET54TransportError


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

    def test_get_info_reports_live_resistance_setpoint(self):
        driver, fake = make_driver(["R800.00\n"])
        info = driver.get_info()

        self.assertEqual(info["resistance_setpoint_ohm"], 800.0)
        self.assertNotIn("configured_resistance_ohm", info)
        self.assertEqual(fake.writes, ["RESI:CR?\n"])

    def test_get_info_returns_none_when_live_resistance_query_fails(self):
        driver, fake = make_driver(["Rcmd err\n"])
        info = driver.get_info()

        self.assertIsNone(info["resistance_setpoint_ohm"])
        self.assertNotIn("configured_resistance_ohm", info)
        self.assertEqual(info["uid"], "bb-solar-load-001")
        self.assertEqual(info["configured_mode"], "CR")
        self.assertEqual(fake.writes, ["RESI:CR?\n"])

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

    def test_active_polling_reading_uses_measure_all_values(self):
        driver, fake = make_driver(["R 0.009 16.000 0.144 1800.0\n"])
        reading = driver.get_reading()
        self.assertEqual(reading["data"]["current_a"], 0.009)
        self.assertEqual(reading["data"]["power_w"], 0.144)
        self.assertEqual(reading["data"]["load_resistance_ohm"], 1800.0)
        self.assertEqual(fake.writes, ["MEAS:ALL?\n"])

    def test_nonphysical_resistance_sentinel_is_unavailable_but_vip_remain_valid(self):
        driver, _fake = make_driver(["R 0.009 16.000 0.144 99999999\n"])
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["data"]["current_a"], 0.009)
        self.assertEqual(reading["data"]["power_w"], 0.144)
        self.assertIsNone(reading["data"]["load_resistance_ohm"])

    def test_invalid_response_returns_null_channels(self):
        driver, _fake = make_driver(["R nonsense\n"])
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "error")
        self.assertTrue(all(value is None for value in reading["data"].values()))

    def test_transport_failure_returns_unavailable_and_resets_connection(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        replacement = FakeSerial([
            "REast Tester,ET5406A+\n", "Rexecu success\n", "ROFF\n",
            OSError(6, "Device not configured"),
        ])
        connections = iter([stale, replacement])
        driver = ET54Driver(
            uid="bb-solar-load-001",
            port="/dev/fake-et54",
            serial_factory=lambda **_kwargs: next(connections),
            reconnect_interval_s=0,
        )
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "node_unavailable")
        self.assertTrue(all(value is None for value in reading["data"].values()))
        self.assertFalse(stale.is_open)
        self.assertFalse(replacement.is_open)
        self.assertIsNone(driver._instrument)

    def test_read_only_query_reconnects_once_with_identity_check(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        replacement = FakeSerial([
            "REast Tester,ET5406A+\n", "Rexecu success\n", "ROFF\n",
            "R 0.010 16.245 0.16 1680\n",
        ])
        opened = []

        def factory(**_kwargs):
            connection = [stale, replacement][len(opened)]
            opened.append(connection)
            return connection

        driver = ET54Driver("load-001", "/dev/cu.old", serial_factory=factory, reconnect_interval_s=0)
        self.assertEqual(driver.measure_all()["load_resistance_ohm"], 1680)
        self.assertEqual(len(opened), 2)
        self.assertFalse(stale.is_open)
        self.assertIs(driver._instrument, replacement)
        self.assertEqual(replacement.writes, ["*IDN?\n", "CH:SW OFF\n", "CH:SW?\n", "MEAS:ALL?\n"])

    def test_read_only_retry_is_bounded(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        replacement = FakeSerial([
            "REast Tester,ET5406A+\n", "Rexecu success\n", "ROFF\n",
            OSError(6, "Device not configured"),
        ])
        opened = []

        def factory(**_kwargs):
            connection = [stale, replacement][len(opened)]
            opened.append(connection)
            return connection

        driver = ET54Driver("load-001", "/dev/cu.old", serial_factory=factory, reconnect_interval_s=0)
        with self.assertRaises(ET54TransportError):
            driver.measure_all()
        self.assertEqual(len(opened), 2)

    def test_input_on_is_not_replayed_after_transport_failure(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        open_count = 0

        def factory(**_kwargs):
            nonlocal open_count
            open_count += 1
            return stale

        driver = ET54Driver("load-001", "/dev/cu.old", serial_factory=factory)
        with self.assertRaises(ET54TransportError):
            driver.input_on()
        self.assertEqual(stale.writes, ["CH:SW ON\n"])
        self.assertEqual(open_count, 1)

    def test_reconnect_rejects_wrong_identity(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        wrong = FakeSerial(["ROther Vendor,Power Supply\n"])
        connections = iter([stale, wrong])
        driver = ET54Driver(
            "load-001", "/dev/cu.old", serial_factory=lambda **_kwargs: next(connections), reconnect_interval_s=0
        )
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "error")
        self.assertEqual(driver.transport_state, "fault")
        self.assertFalse(wrong.is_open)

    def test_changed_port_is_rediscovered_and_identity_verified(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        replacement = FakeSerial([
            "REast Tester,ET5406A+\n", "Rexecu success\n", "ROFF\n",
            "R 0.010 16.245 0.16 1680\n",
        ])
        opened_ports = []

        def factory(**kwargs):
            opened_ports.append(kwargs["port"])
            return stale if kwargs["port"] == "/dev/cu.old" else replacement

        ports = lambda: [SimpleNamespace(device="/dev/cu.new", vid=0x1A86, pid=0x7523, description="CH340")]
        driver = ET54Driver(
            "load-001", "/dev/cu.old", serial_factory=factory, port_lister=ports, reconnect_interval_s=0
        )
        self.assertEqual(driver.measure_all()["current_a"], 0.010)
        self.assertEqual(driver.port, "/dev/cu.new")
        self.assertEqual(opened_ports, ["/dev/cu.old", "/dev/cu.new"])

    def test_ambiguous_rediscovery_does_not_guess(self):
        stale = FakeSerial([OSError(6, "Device not configured")])
        ports = lambda: [
            SimpleNamespace(device="/dev/cu.a", vid=0x1A86, pid=0x7523, description="CH340"),
            SimpleNamespace(device="/dev/cu.b", vid=0x1A86, pid=0x7523, description="CH340"),
        ]
        driver = ET54Driver(
            "load-001", "/dev/cu.old", serial_factory=lambda **_kwargs: stale,
            port_lister=ports, reconnect_interval_s=0,
        )
        reading = driver.get_reading()
        self.assertEqual(reading["status"], "node_unavailable")
        self.assertIn("ambiguous", reading["message"])
        self.assertEqual(driver.port, "/dev/cu.old")

    def test_safe_off_reconnects_verifies_identity_and_confirms_off(self):
        connection = FakeSerial([
            "REast Tester,ET5406A+\n",
            "Rexecu success\n",
            "ROFF\n",
        ])
        driver = ET54Driver("load-001", "/dev/cu.et54", serial_factory=lambda **_kwargs: connection)
        driver.ensure_safe_off()
        self.assertEqual(connection.writes, ["*IDN?\n", "CH:SW OFF\n", "CH:SW?\n"])
        self.assertEqual(driver.transport_message, "ET54 reconnected; load confirmed OFF")

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

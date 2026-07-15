import socket
import unittest
from unittest.mock import patch

from raspi.drivers.wifi_node_driver import WiFiNodeDriver, WiFiNodeDriverError


class FakeConnection:
    def __init__(self, response: str):
        self._chunks = [response.encode("utf-8")]
        self.sent = b""
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, payload):
        self.sent += payload

    def recv(self, size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class WiFiNodeDriverTest(unittest.TestCase):
    def make_driver(self):
        return WiFiNodeDriver(
            uid="bb-solar-pnl-001",
            host="192.0.2.10",
            port=1234,
            timeout_s=3,
        )

    def test_valid_header_and_reading_parse(self):
        driver = self.make_driver()
        responses = [
            FakeConnection("uid,panel_voltage_v,voltage_ok,wifi,rssi_dbm,firmware,extra\n"),
            FakeConnection("bb-solar-pnl-001,7.42,1,1,-61,v1.0,preserved\n"),
        ]

        with patch("raspi.drivers.wifi_node_driver.socket.create_connection", side_effect=responses):
            reading = driver.get_reading()

        self.assertEqual(reading["uid"], "bb-solar-pnl-001")
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["data"]["panel_voltage_v"], 7.42)
        self.assertEqual(reading["extended"]["voltage_ok"], 1)
        self.assertEqual(reading["extended"]["wifi"], 1)
        self.assertEqual(reading["extended"]["rssi_dbm"], -61)
        self.assertEqual(reading["extended"]["firmware"], "v1.0")
        self.assertEqual(reading["extended"]["extra"], "preserved")

    def test_mismatched_field_counts_raise_useful_error(self):
        driver = self.make_driver()
        responses = [
            FakeConnection("uid,panel_voltage_v,voltage_ok\n"),
            FakeConnection("bb-solar-pnl-001,7.42\n"),
        ]

        with patch("raspi.drivers.wifi_node_driver.socket.create_connection", side_effect=responses):
            with self.assertRaisesRegex(WiFiNodeDriverError, "Malformed CSV"):
                driver.get_reading()

    def test_numeric_conversion(self):
        driver = self.make_driver()
        parsed = driver._parse_csv_reading(
            "uid,panel_voltage_v,voltage_ok,wifi,rssi_dbm,count,label",
            "bb-solar-pnl-001,0.25,1,0,-70,12,node-a",
        )

        self.assertIsInstance(parsed["panel_voltage_v"], float)
        self.assertEqual(parsed["panel_voltage_v"], 0.25)
        self.assertIsInstance(parsed["voltage_ok"], int)
        self.assertEqual(parsed["count"], 12)
        self.assertEqual(parsed["label"], "node-a")

    def test_uid_mismatch_raises_clear_error(self):
        driver = self.make_driver()
        responses = [
            FakeConnection("uid,panel_voltage_v\n"),
            FakeConnection("other-node,7.42\n"),
        ]

        with patch("raspi.drivers.wifi_node_driver.socket.create_connection", side_effect=responses):
            with self.assertRaisesRegex(WiFiNodeDriverError, "UID mismatch"):
                driver.get_reading()

    def test_connection_timeout_raises_clear_error(self):
        driver = self.make_driver()

        with patch(
            "raspi.drivers.wifi_node_driver.socket.create_connection",
            side_effect=socket.timeout("timed out"),
        ):
            with self.assertRaisesRegex(WiFiNodeDriverError, "Timed out"):
                driver._send_command("PING")

    def test_connection_refused_raises_clear_error(self):
        driver = self.make_driver()

        with patch(
            "raspi.drivers.wifi_node_driver.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            with self.assertRaisesRegex(WiFiNodeDriverError, "Connection refused"):
                driver._send_command("PING")


if __name__ == "__main__":
    unittest.main()

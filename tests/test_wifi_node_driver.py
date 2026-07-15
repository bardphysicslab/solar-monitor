import socket
import unittest
from unittest.mock import patch

from raspi.drivers.wifi_node_driver import WiFiNodeDriver, WiFiNodeDriverError


INFO = (
    "OK INFO uid=bb-solar-pnl-001 node_type=solar_panel "
    "node_model=dummy_voltage_wifi fw=0.1.0 protocol=bardbox-node-v1 "
    "sensors=DUMMY_VOLTAGE ip=192.168.50.147 mac=B4:3A:45:33:BD:90 rssi_dbm=-38\n"
)


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

    def test_valid_framed_protocol_parse(self):
        driver = self.make_driver()
        responses = [
            FakeConnection(INFO),
            FakeConnection("HDR,v1,panel_voltage_v,voltage_ok,rssi_dbm\n"),
            FakeConnection("DAT,9.497,1,-46\n"),
        ]

        with patch("raspi.drivers.wifi_node_driver.socket.create_connection", side_effect=responses):
            reading = driver.get_reading()

        self.assertEqual(reading["uid"], "bb-solar-pnl-001")
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["data"]["panel_voltage_v"], 9.497)
        self.assertEqual(reading["extended"]["voltage_ok"], 1)
        self.assertEqual(reading["extended"]["rssi_dbm"], -46)
        self.assertEqual(reading["extended"]["record_version"], "v1")
        self.assertEqual(reading["extended"]["node_uid"], "bb-solar-pnl-001")
        self.assertEqual(reading["extended"]["fw"], "0.1.0")
        self.assertEqual(reading["extended"]["protocol"], "bardbox-node-v1")
        self.assertEqual(reading["extended"]["ip"], "192.168.50.147")
        self.assertEqual(reading["extended"]["mac"], "B4:3A:45:33:BD:90")
        self.assertEqual(reading["extended"]["sensors"], "DUMMY_VOLTAGE")

    def test_bad_header_prefix(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "Unexpected HEADER prefix"):
            driver._parse_csv_reading(
                "HEAD,v1,panel_voltage_v,voltage_ok,rssi_dbm",
                "DAT,9.497,1,-46",
            )

    def test_bad_read_prefix(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "Unexpected READ prefix"):
            driver._parse_csv_reading(
                "HDR,v1,panel_voltage_v,voltage_ok,rssi_dbm",
                "DATA,9.497,1,-46",
            )

    def test_missing_header_version(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "missing record-format version"):
            driver._parse_csv_reading(
                "HDR,,panel_voltage_v,voltage_ok,rssi_dbm",
                "DAT,9.497,1,-46",
            )

    def test_zero_measurement_columns(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "no measurement columns"):
            driver._parse_csv_reading("HDR,v1", "DAT,9.497")

    def test_mismatched_post_framing_field_counts(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "HEADER has 3 measurement fields, READ has 2 values"):
            driver._parse_csv_reading(
                "HDR,v1,panel_voltage_v,voltage_ok,rssi_dbm",
                "DAT,9.497,1",
            )

    def test_empty_response(self):
        driver = self.make_driver()

        with self.assertRaisesRegex(WiFiNodeDriverError, "Empty HEADER response"):
            driver._parse_csv_reading("", "DAT,9.497,1,-46")

    def test_uid_mismatch_raises_clear_error(self):
        driver = self.make_driver()
        responses = [
            FakeConnection(INFO.replace("bb-solar-pnl-001", "other-node")),
        ]

        with patch("raspi.drivers.wifi_node_driver.socket.create_connection", side_effect=responses):
            with self.assertRaisesRegex(WiFiNodeDriverError, "UID mismatch"):
                driver.get_info()

    def test_valid_ok_info_key_value_parsing(self):
        driver = self.make_driver()
        parsed = driver._parse_info_response(INFO)

        self.assertEqual(parsed["uid"], "bb-solar-pnl-001")
        self.assertEqual(parsed["node_type"], "solar_panel")
        self.assertEqual(parsed["node_model"], "dummy_voltage_wifi")
        self.assertEqual(parsed["fw"], "0.1.0")
        self.assertEqual(parsed["protocol"], "bardbox-node-v1")
        self.assertEqual(parsed["sensors"], "DUMMY_VOLTAGE")
        self.assertEqual(parsed["ip"], "192.168.50.147")
        self.assertEqual(parsed["mac"], "B4:3A:45:33:BD:90")
        self.assertEqual(parsed["rssi_dbm"], -38)

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

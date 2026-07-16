import json
import os
import unittest

os.environ.setdefault("BARDBOX_APP_CONFIG", "raspi/config/app_config.example.json")

import raspi.main as main
from raspi.drivers.spn1_driver import SPN1Driver
from raspi.drivers.wifi_node_driver import WiFiNodeDriver


class MainMultiDeviceTest(unittest.TestCase):
    def setUp(self):
        self.original_drivers = main.DRIVERS
        self.original_primary = main.PRIMARY_DRIVER
        self.original_run_active = main.run_active
        self.original_readings = dict(main.latest_readings_by_uid)

        self.spn1 = SPN1Driver(uid="spn1-0001", port="/dev/null", baud=9600)
        self.wifi = WiFiNodeDriver(uid="bb-solar-pnl-001", host="192.0.2.10")

        self.spn1_reading = {
            "uid": "spn1-0001",
            "timestamp": "2026-07-15T12:00:00Z",
            "status": "ok",
            "data": {"total_w_m2": 10.1, "diffuse_w_m2": 4.2, "sun": 1},
            "extended": {},
            "raw": "10.1,4.2,1",
        }
        self.wifi_reading = {
            "uid": "bb-solar-pnl-001",
            "timestamp": "2026-07-15T12:00:01Z",
            "status": "ok",
            "data": {"panel_voltage_v": 9.497},
            "extended": {"voltage_ok": 1, "rssi_dbm": -46, "fw": "0.1.0", "wifi": 1},
            "raw": {"header": "HDR,v1,panel_voltage_v,voltage_ok,rssi_dbm", "read": "DAT,9.497,1,-46"},
        }

        self.spn1.get_reading = lambda: self.spn1_reading
        self.wifi.get_reading = lambda: self.wifi_reading
        self.wifi.get_info = lambda: {
            "uid": "bb-solar-pnl-001",
            "driver": "wifi_node",
            "transport": "wifi_tcp",
            "connection_state": "ok",
            "fw": "0.1.0",
        }

        main.DRIVERS = [self.spn1, self.wifi]
        main.PRIMARY_DRIVER = self.spn1
        main.latest_readings_by_uid = {}
        main.run_active = False

    def tearDown(self):
        main.DRIVERS = self.original_drivers
        main.PRIMARY_DRIVER = self.original_primary
        main.run_active = self.original_run_active
        main.latest_readings_by_uid = self.original_readings

    def test_spn1_remains_configured_with_wifi_enabled(self):
        self.assertTrue(any(isinstance(driver, SPN1Driver) for driver in main.DRIVERS))
        self.assertTrue(any(isinstance(driver, WiFiNodeDriver) for driver in main.DRIVERS))

    def test_start_and_stop_toggle_global_polling_state(self):
        main.start_run()
        self.assertTrue(main.is_run_active())

        main.stop_run()
        self.assertFalse(main.is_run_active())

    def test_spn1_routes_still_exist(self):
        paths = {route.path for route in main.app.routes}

        self.assertIn("/spn1/status", paths)
        self.assertIn("/spn1/time", paths)
        self.assertIn("/spn1/time/sync", paths)
        self.assertIn("/start", paths)
        self.assertIn("/stop", paths)

    def test_configured_wifi_nodes_are_config_derived(self):
        nodes = main.configured_wifi_nodes(
            {
                "drivers": [
                    {"driver": "wifi_node", "uid": "bb-solar-pnl-001", "config": {"host": "192.0.2.10", "port": 1234}},
                    {"driver": "spn1", "uid": "spn1-0001", "config": {"port": "/dev/null"}},
                ]
            }
        )

        self.assertEqual(
            nodes,
            [
                {
                    "uid": "bb-solar-pnl-001",
                    "driver": "wifi_node",
                    "host": "192.0.2.10",
                    "port": 1234,
                }
            ],
        )

    def test_wifi_parse_error_does_not_remove_or_overwrite_spn1_reading(self):
        self.wifi.get_reading = lambda: (_ for _ in ()).throw(ValueError("bad Wi-Fi frame"))

        main.poll_all_drivers_once()

        readings = {reading["uid"]: reading for reading in main.latest_readings()}
        self.assertEqual(readings["spn1-0001"]["status"], "ok")
        self.assertEqual(readings["spn1-0001"]["data"]["total_w_m2"], 10.1)
        self.assertEqual(readings["bb-solar-pnl-001"]["status"], "error")
        self.assertIn("bad Wi-Fi frame", readings["bb-solar-pnl-001"]["extended"]["error"])

    def test_both_spn1_and_wifi_readings_can_appear_in_latest_endpoint(self):
        main.poll_all_drivers_once()

        response = main.get_latest_readings()
        payload = json.loads(response.body)
        readings = {reading["uid"]: reading for reading in payload["readings"]}

        self.assertEqual(readings["spn1-0001"]["data"]["total_w_m2"], 10.1)
        self.assertEqual(readings["bb-solar-pnl-001"]["data"]["panel_voltage_v"], 9.497)


if __name__ == "__main__":
    unittest.main()

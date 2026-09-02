import json
import inspect
import os
import threading
import time
import unittest

os.environ.setdefault("BARDBOX_APP_CONFIG", "raspi/config/app_config.example.json")

import raspi.main as main
from raspi.drivers.et54_driver import ET54Driver
from raspi.drivers.spn1_driver import SPN1Driver
from raspi.drivers.wifi_node_driver import WiFiNodeDriver


class FakeRecorder:
    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.samples = []
        self.flush_due_calls = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def add_reading(self, uid, reading):
        self.samples.append((uid, reading))

    def flush_due(self):
        self.flush_due_calls += 1

    def flush_all(self):
        pass

    def status(self):
        return {"recording_enabled": False, "data_root": "/tmp/test", "drivers": {}}


class FakeBackupManager:
    def status(self):
        return {"enabled": False, "status": "ok"}


class MainMultiDeviceTest(unittest.TestCase):
    def setUp(self):
        self.original_drivers = main.DRIVERS
        self.original_primary = main.PRIMARY_DRIVER
        self.original_run_active = main.run_active
        self.original_readings = dict(main.latest_readings_by_uid)
        self.original_successful_readings = dict(main.latest_successful_readings_by_uid)
        self.original_signatures = dict(main.last_recorded_signatures_by_uid)
        self.original_recorder = main.RECORDER
        self.original_backup_manager = main.BACKUP_MANAGER
        self.original_load_controllers = main.LOAD_CONTROLLERS

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
        main.latest_successful_readings_by_uid = {}
        main.last_recorded_signatures_by_uid = {}
        main.run_active = False
        main.RECORDER = FakeRecorder()
        main.BACKUP_MANAGER = FakeBackupManager()
        main.LOAD_CONTROLLERS = {}

    def tearDown(self):
        main.DRIVERS = self.original_drivers
        main.PRIMARY_DRIVER = self.original_primary
        main.run_active = self.original_run_active
        main.latest_readings_by_uid = self.original_readings
        main.latest_successful_readings_by_uid = self.original_successful_readings
        main.last_recorded_signatures_by_uid = self.original_signatures
        main.RECORDER = self.original_recorder
        main.BACKUP_MANAGER = self.original_backup_manager
        main.LOAD_CONTROLLERS = self.original_load_controllers

    def test_spn1_remains_configured_with_wifi_enabled(self):
        self.assertTrue(any(isinstance(driver, SPN1Driver) for driver in main.DRIVERS))
        self.assertTrue(any(isinstance(driver, WiFiNodeDriver) for driver in main.DRIVERS))

    def test_start_and_stop_toggle_global_polling_state(self):
        main.start_run()
        self.assertTrue(main.is_run_active())
        self.assertEqual(main.RECORDER.started, 1)

        main.stop_run()
        self.assertFalse(main.is_run_active())
        self.assertEqual(main.RECORDER.stopped, 1)

    def test_spn1_routes_still_exist(self):
        paths = {route.path for route in main.app.routes}

        self.assertIn("/spn1/status", paths)
        self.assertIn("/spn1/time", paths)
        self.assertIn("/spn1/time/sync", paths)
        self.assertIn("/start", paths)
        self.assertIn("/stop", paths)

    def test_application_does_not_schedule_backup_loop(self):
        self.assertFalse(hasattr(main, "backup_loop"))

    def test_generic_background_measurement_cadence_is_ten_seconds(self):
        self.assertEqual(inspect.signature(main.generic_polling_loop).parameters["poll_interval_s"].default, 10.0)

    def test_disconnected_configured_load_is_due_for_bounded_background_reconnect(self):
        class Driver:
            uid = "load-001"

        class Controller:
            def state(self):
                return {
                    "input_enabled": False,
                    "sweep_state": "idle",
                    "transport_state": "disconnected",
                }

        driver = Driver()
        main.LOAD_CONTROLLERS = {driver.uid: Controller()}
        self.assertEqual(main.drivers_due_for_polling([driver], run_is_active=False), [driver])

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
                    "source_location": "network",
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
        self.assertEqual([uid for uid, _reading in main.RECORDER.samples], ["spn1-0001", "bb-solar-pnl-001"])
        self.assertEqual(main.RECORDER.flush_due_calls, 1)

    def test_repeated_cached_reading_is_not_recorded_twice(self):
        main.poll_driver_once(self.spn1)
        main.poll_driver_once(self.spn1)

        self.assertEqual(len(main.RECORDER.samples), 1)

    def test_spn1_blocking_does_not_prevent_wifi_polling_loop(self):
        calls = []

        def slow_spn1():
            time.sleep(0.2)
            calls.append("spn1")
            return self.spn1_reading

        self.spn1.get_reading = slow_spn1
        main.run_active = True
        stop_event = threading.Event()
        thread = threading.Thread(target=main.spn1_acquisition_loop, args=(stop_event,))
        thread.start()

        main.poll_driver_once(self.wifi)
        stop_event.set()
        thread.join(timeout=1)

        self.assertIn(("bb-solar-pnl-001", self.wifi_reading), main.RECORDER.samples)

    def test_et54_configuration_loads_as_generic_polled_driver(self):
        drivers = main.load_drivers(
            {
                "drivers": [
                    {
                        "driver": "et54",
                        "uid": "yertai-et5406a-plus-001",
                        "config": {
                            "port": "/dev/serial/by-id/test-et54",
                            "baud": 9600,
                            "mode": "CR",
                            "resistance_ohm": 800,
                            "recording": {
                                "enabled": True,
                                "interval_s": 10,
                                "mode": "mean",
                            },
                        },
                    }
                ]
            }
        )
        self.assertEqual(len(drivers), 1)
        self.assertIsInstance(drivers[0], ET54Driver)
        self.assertEqual(drivers[0].uid, "yertai-et5406a-plus-001")
        self.assertEqual(drivers[0].port, "/dev/serial/by-id/test-et54")
        self.assertEqual(drivers[0].resistance_ohm, 800.0)
        self.assertIsNone(drivers[0]._instrument)

        main.DRIVERS = [self.spn1, drivers[0], self.wifi]
        self.assertEqual(main.polled_drivers(), [drivers[0], self.wifi])

    def test_panel_load_association_is_explicit_and_unassigned_panels_remain_unassigned(self):
        nodes = main.configured_wifi_nodes(
            {
                "drivers": [
                    {"driver": "wifi_node", "uid": "panel-001", "config": {"host": "192.0.2.1"}},
                    {"driver": "wifi_node", "uid": "panel-002", "config": {"host": "192.0.2.2"}},
                    {
                        "driver": "et54",
                        "uid": "load-001",
                        "config": {"panel_uid": "panel-001", "load_type": "electronic_load"},
                    },
                ]
            }
        )
        by_uid = {node["uid"]: node for node in nodes}
        self.assertEqual(by_uid["panel-001"]["load"]["uid"], "load-001")
        self.assertEqual(by_uid["panel-001"]["load"]["load_type"], "electronic_load")
        self.assertEqual(by_uid["panel-001"]["source_location"], "local")
        self.assertNotIn("load", by_uid["panel-002"])
        self.assertEqual(by_uid["panel-002"]["source_location"], "network")

    def test_load_api_uses_latest_associated_et54_reading_atomically(self):
        class ActiveController:
            uid = "yertai-et5406a-plus-001"

            def state(self):
                return {
                    "uid": self.uid,
                    "panel_uid": "bb-solar-pnl-001",
                    "active_mode": "fixed_resistance",
                    "input_enabled": True,
                    "applied_resistance_ohm": 1800.0,
                    "last_measurement": {
                        "voltage_v": 16.255,
                        "current_a": 0.0,
                        "power_w": 0.0,
                        "load_resistance_ohm": None,
                    },
                }

        controller = ActiveController()
        main.LOAD_CONTROLLERS = {controller.uid: controller}
        main.set_latest_reading(controller.uid, {
            "uid": controller.uid,
            "status": "ok",
            "data": {
                "voltage_v": 16.246,
                "current_a": 0.010,
                "power_w": 0.16,
                "load_resistance_ohm": 1686.3,
            },
        })

        response = main.get_loads()
        payload = json.loads(response.body)["loads"][0]
        self.assertEqual(
            payload["live_reading"],
            {
                "voltage_v": 16.246,
                "current_a": 0.010,
                "power_w": 0.16,
                "load_resistance_ohm": 1686.3,
            },
        )
        self.assertNotEqual(payload["live_reading"], payload["last_measurement"])

    def test_background_poll_stores_one_successful_et54_reading_for_load_api(self):
        stop_event = threading.Event()

        class Driver:
            uid = "yertai-et5406a-plus-001"

        class ActiveController:
            uid = Driver.uid

            def __init__(self):
                self.poll_count = 0

            def state(self):
                return {
                    "uid": self.uid,
                    "sweep_state": "idle",
                    "input_enabled": True,
                    "active_mode": "fixed_resistance",
                    "last_measurement": {
                        "voltage_v": 16.253,
                        "current_a": 0.0,
                        "power_w": 0.0,
                        "load_resistance_ohm": None,
                    },
                }

            def poll_reading(self):
                self.poll_count += 1
                stop_event.set()
                return {
                    "uid": self.uid,
                    "timestamp": "2026-09-01T16:00:00Z",
                    "status": "ok",
                    "data": {
                        "voltage_v": 16.246,
                        "current_a": 0.010,
                        "power_w": 0.16,
                        "load_resistance_ohm": 1686.3,
                    },
                    "extended": {},
                    "raw": "one ET54 frame",
                }

        driver = Driver()
        controller = ActiveController()
        main.DRIVERS = [driver]
        main.LOAD_CONTROLLERS = {driver.uid: controller}
        main.run_active = False

        self.assertEqual(main.drivers_due_for_polling([driver], False), [driver])
        main.generic_polling_loop(stop_event, poll_interval_s=0.01)

        response = main.get_loads()
        payload = json.loads(response.body)["loads"][0]
        self.assertEqual(controller.poll_count, 1)
        self.assertEqual(
            payload["live_reading"],
            {
                "voltage_v": 16.246,
                "current_a": 0.010,
                "power_w": 0.16,
                "load_resistance_ohm": 1686.3,
            },
        )
        self.assertEqual(main.RECORDER.samples, [])

    def test_failed_poll_preserves_previous_successful_reading_and_exposes_error(self):
        class Controller:
            uid = "yertai-et5406a-plus-001"

            def state(self):
                return {
                    "uid": self.uid,
                    "last_measurement": {
                        "voltage_v": 16.255,
                        "current_a": 0.0,
                        "power_w": 0.0,
                        "load_resistance_ohm": None,
                    },
                }

        controller = Controller()
        main.LOAD_CONTROLLERS = {controller.uid: controller}
        main.set_latest_reading(controller.uid, {
            "uid": controller.uid,
            "status": "ok",
            "data": {
                "voltage_v": 16.246,
                "current_a": 0.010,
                "power_w": 0.16,
                "load_resistance_ohm": 1686.3,
            },
        })
        main.set_latest_reading(controller.uid, {
            "uid": controller.uid,
            "status": "error",
            "extended": {"error": "serial timeout"},
        })

        response = main.get_loads()
        payload = json.loads(response.body)["loads"][0]
        self.assertEqual(
            payload["live_reading"],
            {
                "voltage_v": 16.246,
                "current_a": 0.010,
                "power_w": 0.16,
                "load_resistance_ohm": 1686.3,
            },
        )
        self.assertEqual(payload["panel_reading"], payload["live_reading"])
        self.assertEqual(payload["poll_status"], "error")
        self.assertEqual(payload["poll_error"], "serial timeout")

    def test_successive_fixed_polls_replace_panel_reading_atomically(self):
        class Controller:
            uid = "load-001"

            def state(self):
                return {"uid": self.uid, "selected_mode": "fixed_resistance", "input_enabled": True}

        controller = Controller()
        main.LOAD_CONTROLLERS = {controller.uid: controller}
        first = {"voltage_v": 16.2, "current_a": 0.010, "power_w": 0.16, "load_resistance_ohm": 1680.0}
        second = {"voltage_v": 15.9, "current_a": 0.009, "power_w": 0.14, "load_resistance_ohm": 1766.7}
        main.set_latest_reading(controller.uid, {"status": "ok", "data": first})
        self.assertEqual(json.loads(main.get_loads().body)["loads"][0]["panel_reading"], first)
        main.set_latest_reading(controller.uid, {"status": "ok", "data": second})
        payload = json.loads(main.get_loads().body)["loads"][0]
        self.assertEqual(payload["panel_reading"], second)
        self.assertEqual(payload["panel_reading_source"], "et54_poll")

    def test_sweep_mode_uses_latest_complete_mpp_atomically(self):
        class Controller:
            uid = "load-001"

            def state(self):
                return {
                    "uid": self.uid,
                    "selected_mode": "sweep",
                    "sweep_state": "running",
                    "last_successful_sweep": {
                        "electrical_status": "complete",
                        "vmpp_v": 15.8,
                        "impp_a": 0.018,
                        "pmpp_w": 0.2844,
                        "rmpp_ohm": 877.8,
                        "points": [{"voltage_v": 12.0, "current_a": 0.02}],
                    },
                }

        controller = Controller()
        main.LOAD_CONTROLLERS = {controller.uid: controller}
        main.set_latest_reading(controller.uid, {
            "status": "ok",
            "data": {"voltage_v": 99, "current_a": 99, "power_w": 99, "load_resistance_ohm": 99},
        })
        payload = json.loads(main.get_loads().body)["loads"][0]
        self.assertEqual(
            payload["panel_reading"],
            {"voltage_v": 15.8, "current_a": 0.018, "power_w": 0.2844, "load_resistance_ohm": 877.8},
        )
        self.assertEqual(payload["panel_reading_source"], "sweep_mpp")

    def test_running_sweep_is_skipped_without_blocking_other_generic_drivers(self):
        class RunningController:
            def state(self):
                return {"sweep_state": "running"}

        et54 = ET54Driver(uid="load-001", port="/dev/null")
        main.DRIVERS = [et54, self.wifi]
        main.LOAD_CONTROLLERS = {"load-001": RunningController()}
        main.run_active = True
        stop_event = threading.Event()
        original_wifi_reading = self.wifi.get_reading

        def wifi_reading_and_stop():
            stop_event.set()
            return original_wifi_reading()

        self.wifi.get_reading = wifi_reading_and_stop
        thread = threading.Thread(target=main.generic_polling_loop, args=(stop_event, 0.01))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertIn(("bb-solar-pnl-001", self.wifi_reading), main.RECORDER.samples)

    def test_continuous_sweep_wait_does_not_allow_ordinary_et54_poll(self):
        class SweepController:
            def state(self):
                return {"sweep_state": "completed", "sweep_run_active": True}

            def poll_reading(self):
                raise AssertionError("ordinary poll must remain suspended between continuous sweeps")

        et54 = ET54Driver(uid="load-001", port="/dev/null")
        main.DRIVERS = [et54, self.wifi]
        main.LOAD_CONTROLLERS = {"load-001": SweepController()}
        main.run_active = True
        stop_event = threading.Event()
        original_wifi_reading = self.wifi.get_reading

        def wifi_reading_and_stop():
            stop_event.set()
            return original_wifi_reading()

        self.wifi.get_reading = wifi_reading_and_stop
        main.generic_polling_loop(stop_event, poll_interval_s=0.01)
        self.assertIn(("bb-solar-pnl-001", self.wifi_reading), main.RECORDER.samples)

    def test_unsafe_sweep_is_rejected_before_background_thread_starts(self):
        class UnsafeController:
            def state(self):
                return {"sweep_state": "idle", "sweep_run_active": False}

            def validate_sweep_configuration(self):
                raise main.LoadControlError("panel operating limits are required")

        main.sweep_threads.pop("load-001", None)
        main.LOAD_CONTROLLERS = {"load-001": UnsafeController()}
        response = main.start_load_sweep("load-001")
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("panel operating limits", payload["error"])
        self.assertNotIn("load-001", main.sweep_threads)


if __name__ == "__main__":
    unittest.main()

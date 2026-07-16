import os
import threading
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BARDBOX_APP_CONFIG", "raspi/config/app_config.example.json")

import raspi.main as main
from raspi.drivers.spn1_driver import SPN1Driver


class TrackingRLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0
        self.max_depth = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        self._lock.release()
        return False


class FakeSPN1Driver(SPN1Driver):
    def __init__(self, **kwargs):
        super().__init__(uid="spn1-0001", port="/dev/null", baud=9600, **kwargs)
        self.sync_calls = []

    def sync_device_time(self, dt_utc):
        self.sync_calls.append(dt_utc)
        return {"status": "ok", "timestamp": main.iso_utc(dt_utc), "time_basis": "UTC", "error": None}


class SPN1TimeSyncTest(unittest.TestCase):
    def setUp(self):
        self.original_drivers = main.DRIVERS
        self.original_primary = main.PRIMARY_DRIVER
        self.original_status = dict(main.spn1_sync_status)

    def tearDown(self):
        main.DRIVERS = self.original_drivers
        main.PRIMARY_DRIVER = self.original_primary
        main.spn1_sync_status.clear()
        main.spn1_sync_status.update(self.original_status)

    def test_default_auto_sync_and_interval(self):
        drivers = main.load_drivers(
            {
                "drivers": [
                    {"driver": "spn1", "uid": "spn1-0001", "config": {"port": "/dev/null", "baud": 9600}}
                ]
            }
        )

        self.assertTrue(drivers[0].auto_sync_time)
        self.assertEqual(drivers[0].sync_interval_hours, 24)

    def test_disabled_auto_sync(self):
        drivers = main.load_drivers(
            {
                "drivers": [
                    {
                        "driver": "spn1",
                        "uid": "spn1-0001",
                        "config": {"port": "/dev/null", "baud": 9600, "auto_sync_time": False},
                    }
                ]
            }
        )

        self.assertFalse(drivers[0].auto_sync_time)

    def test_invalid_interval_falls_back_to_24(self):
        self.assertEqual(main.parse_spn1_sync_interval_hours(0, "spn1-0001"), 24)
        self.assertEqual(main.parse_spn1_sync_interval_hours(-1, "spn1-0001"), 24)
        self.assertEqual(main.parse_spn1_sync_interval_hours("not-a-number", "spn1-0001"), 24)

    def test_successful_startup_sync_records_status(self):
        driver = FakeSPN1Driver()
        main.DRIVERS = [driver]
        main.PRIMARY_DRIVER = driver
        main.configure_initial_spn1_sync_status()

        result = main.sync_spn1_time_once(reason="startup")
        status = main.get_spn1_sync_status()

        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(status["last_sync_attempt_utc"])
        self.assertIsNotNone(status["last_sync_success_utc"])
        self.assertIsNone(status["last_sync_error"])
        self.assertEqual(driver.sync_calls[0].tzinfo, timezone.utc)

    def test_startup_sync_failure_does_not_raise(self):
        driver = FakeSPN1Driver()
        driver.sync_device_time = lambda dt_utc: (_ for _ in ()).throw(RuntimeError("serial unavailable"))
        main.DRIVERS = [driver]
        main.PRIMARY_DRIVER = driver
        main.configure_initial_spn1_sync_status()

        result = main.sync_spn1_time_once(reason="startup")

        self.assertEqual(result["status"], "error")
        self.assertIn("serial unavailable", main.get_spn1_sync_status()["last_sync_error"])

    def test_periodic_sync_due_after_interval_only(self):
        driver = FakeSPN1Driver(sync_interval_hours=2)
        main.DRIVERS = [driver]
        main.PRIMARY_DRIVER = driver
        main.configure_initial_spn1_sync_status()
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

        main.update_spn1_sync_status(last_sync_attempt_utc=main.iso_utc(now - timedelta(hours=1, minutes=59)))
        self.assertFalse(main.should_sync_spn1_time(now, driver))

        main.update_spn1_sync_status(last_sync_attempt_utc=main.iso_utc(now - timedelta(hours=2)))
        self.assertTrue(main.should_sync_spn1_time(now, driver))

    def test_manual_sync_allowed_when_auto_sync_disabled(self):
        driver = FakeSPN1Driver(auto_sync_time=False)
        main.DRIVERS = [driver]
        main.PRIMARY_DRIVER = driver
        main.configure_initial_spn1_sync_status()

        result = main.sync_spn1_time_once(reason="manual")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(driver.sync_calls), 1)

    def test_non_manual_sync_skips_when_disabled(self):
        driver = FakeSPN1Driver(auto_sync_time=False)
        main.DRIVERS = [driver]
        main.PRIMARY_DRIVER = driver
        main.configure_initial_spn1_sync_status()

        result = main.sync_spn1_time_once(reason="periodic")

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(driver.sync_calls, [])

    def test_driver_sync_uses_utc_and_holds_lock(self):
        driver = SPN1Driver(uid="spn1-0001", port="/dev/null", baud=9600)
        tracking_lock = TrackingRLock()
        writes = []

        driver._lock = tracking_lock
        driver._open = lambda read_timeout_s=1.0: object()
        driver._reset_input = lambda instrument: None
        driver._enter_test_mode = lambda: "TEST"
        driver._write_raw = lambda data: writes.append(data)
        driver._read_until = lambda predicate, timeout_s: ""
        driver._read_available_for = lambda duration_s: ""

        def fake_send_command(command, **kwargs):
            self.assertGreater(tracking_lock.depth, 0)
            return "2025/12/31 23:02:03"

        driver._send_command = fake_send_command

        result = driver.sync_device_time(datetime(2026, 1, 1, 1, 2, 3, tzinfo=timezone(timedelta(hours=2))))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["time_basis"], "UTC")
        self.assertIn(b"Y2025/12/31\r", writes)
        self.assertIn(b"H23:02:03\r", writes)
        self.assertGreaterEqual(tracking_lock.max_depth, 1)


if __name__ == "__main__":
    unittest.main()

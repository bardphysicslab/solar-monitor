import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from raspi.drivers.spn1_driver import SPN1Driver
from raspi.drivers.wifi_node_driver import WiFiNodeDriver


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
logger = logging.getLogger(__name__)

app = FastAPI(title="Solar Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

run_active = False
latest_readings_by_uid: Dict[str, Dict[str, Any]] = {}
state_lock = threading.Lock()
sync_status_lock = threading.Lock()
spn1_sync_status: Dict[str, Any] = {
    "auto_sync_enabled": False,
    "sync_interval_hours": 24,
    "last_sync_attempt_utc": None,
    "last_sync_success_utc": None,
    "last_sync_error": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return utc_now().astimezone()


def load_config() -> Dict[str, Any]:
    config_path = Path(os.environ.get("BARDBOX_APP_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


APP_CONFIG = load_config()


def parse_spn1_sync_interval_hours(value: Any, uid: str) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid SPN1 sync_interval_hours for %s; falling back to 24 hours", uid)
        return 24

    if interval <= 0:
        logger.warning("Invalid SPN1 sync_interval_hours for %s; falling back to 24 hours", uid)
        return 24

    return interval


def load_drivers(config: Dict[str, Any]) -> List[Any]:
    loaded = []
    for entry in config.get("drivers", []):
        driver_name = entry.get("driver")
        uid = entry.get("uid", "spn1-0001")
        driver_config = entry.get("config", {})

        if driver_name == "spn1":
            sync_interval_hours = parse_spn1_sync_interval_hours(
                driver_config.get("sync_interval_hours", 24),
                uid,
            )
            loaded.append(
                SPN1Driver(
                    uid=uid,
                    port=driver_config.get("port", "/dev/cu.PL2303G-USBtoUART130"),
                    baud=int(driver_config.get("baud", 9600)),
                    auto_sync_time=bool(driver_config.get("auto_sync_time", True)),
                    sync_interval_hours=sync_interval_hours,
                )
            )
        elif driver_name == "wifi_node":
            host = driver_config.get("host")
            if not host:
                raise ValueError(f"Wi-Fi node driver {uid} requires config.host")

            loaded.append(
                WiFiNodeDriver(
                    uid=uid,
                    host=host,
                    port=int(driver_config.get("port", 1234)),
                    timeout_s=float(driver_config.get("timeout_s", 3.0)),
                )
            )
        else:
            raise ValueError(f"Unsupported driver in solar monitor: {driver_name}")
    return loaded


DRIVERS = load_drivers(APP_CONFIG)
PRIMARY_DRIVER = DRIVERS[0] if DRIVERS else None


def get_configured_spn1_driver() -> Optional[SPN1Driver]:
    for driver in DRIVERS:
        if isinstance(driver, SPN1Driver):
            return driver
    return None


def time_status() -> Dict[str, Any]:
    return {
        "valid": True,
        "source": "system",
        "sane": True,
        "ntp_synced": False,
    }


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def configure_initial_spn1_sync_status() -> None:
    driver = get_configured_spn1_driver()
    with sync_status_lock:
        if driver is None:
            spn1_sync_status.update(
                {
                    "auto_sync_enabled": False,
                    "sync_interval_hours": 24,
                    "last_sync_attempt_utc": None,
                    "last_sync_success_utc": None,
                    "last_sync_error": "SPN1 driver not configured",
                }
            )
            return

        spn1_sync_status.update(
            {
                "auto_sync_enabled": bool(driver.auto_sync_time),
                "sync_interval_hours": driver.sync_interval_hours,
                "last_sync_attempt_utc": None,
                "last_sync_success_utc": None,
                "last_sync_error": None,
            }
        )


def get_spn1_sync_status() -> Dict[str, Any]:
    with sync_status_lock:
        return dict(spn1_sync_status)


def update_spn1_sync_status(**updates: Any) -> None:
    with sync_status_lock:
        spn1_sync_status.update(updates)


def should_sync_spn1_time(now_utc: datetime, driver: SPN1Driver) -> bool:
    status = get_spn1_sync_status()
    last_attempt = status.get("last_sync_attempt_utc")
    if not last_attempt:
        return True

    try:
        last_attempt_dt = datetime.strptime(last_attempt, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    return now_utc - last_attempt_dt >= timedelta(hours=driver.sync_interval_hours)


def sync_spn1_time_once(reason: str = "manual") -> Dict[str, Any]:
    driver = get_configured_spn1_driver()
    if driver is None:
        error = "SPN1 driver not configured"
        update_spn1_sync_status(last_sync_error=error)
        return {"status": "skipped", "error": error}

    update_spn1_sync_status(
        auto_sync_enabled=bool(driver.auto_sync_time),
        sync_interval_hours=driver.sync_interval_hours,
    )

    if not driver.auto_sync_time and reason != "manual":
        logger.info("SPN1 automatic UTC time sync is disabled")
        return {"status": "skipped", "error": None}

    now_utc = utc_now()
    update_spn1_sync_status(last_sync_attempt_utc=iso_utc(now_utc))
    logger.info("Attempting SPN1 UTC time sync (%s)", reason)

    try:
        result = driver.sync_device_time(now_utc)
    except Exception as exc:
        error = str(exc)
        logger.warning("SPN1 UTC time sync failed (%s): %s", reason, error)
        update_spn1_sync_status(last_sync_error=error)
        return {"status": "error", "error": error}

    if result.get("status") == "ok":
        update_spn1_sync_status(
            last_sync_success_utc=iso_utc(utc_now()),
            last_sync_error=None,
        )
        logger.info("SPN1 UTC time sync succeeded (%s)", reason)
    else:
        error = result.get("error") or "SPN1 time sync failed"
        update_spn1_sync_status(last_sync_error=error)
        logger.warning("SPN1 UTC time sync failed (%s): %s", reason, error)

    return result


def spn1_time_sync_loop() -> None:
    driver = get_configured_spn1_driver()
    if driver is None:
        configure_initial_spn1_sync_status()
        return

    configure_initial_spn1_sync_status()

    if driver.auto_sync_time:
        sync_spn1_time_once(reason="startup")

    while True:
        time.sleep(60)
        driver = get_configured_spn1_driver()
        if driver is None:
            configure_initial_spn1_sync_status()
            continue
        if not driver.auto_sync_time:
            update_spn1_sync_status(auto_sync_enabled=False)
            continue

        now_utc = utc_now()
        if should_sync_spn1_time(now_utc, driver):
            logger.info("Periodic SPN1 UTC time sync due")
            sync_spn1_time_once(reason="periodic")


configure_initial_spn1_sync_status()


def set_latest_reading(uid: str, reading: Dict[str, Any]) -> None:
    with state_lock:
        latest_readings_by_uid[uid] = reading


def is_run_active() -> bool:
    with state_lock:
        return run_active


def poll_all_drivers_once() -> None:
    for driver in DRIVERS:
        driver_uid = getattr(driver, "uid", "unknown")
        try:
            reading = driver.get_reading()
            set_latest_reading(driver_uid, reading)
        except Exception as exc:
            logger.warning("Driver polling failed for %s: %s", driver_uid, exc)
            set_latest_reading(
                driver_uid,
                {
                    "uid": driver_uid,
                    "timestamp": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "error",
                    "data": {},
                    "extended": {"error": str(exc)},
                    "raw": None,
                },
            )


def polling_loop() -> None:
    while True:
        if is_run_active() and DRIVERS:
            poll_all_drivers_once()
            time.sleep(1.0)
        else:
            time.sleep(0.2)


def driver_payload(driver: Any) -> Dict[str, Any]:
    return {
        "info": driver.get_info(),
        "capabilities": driver.get_capabilities(),
    }


def latest_reading_for_driver(driver: Any) -> Optional[Dict[str, Any]]:
    driver_uid = getattr(driver, "uid", None)
    if driver_uid is None:
        return None

    with state_lock:
        return latest_readings_by_uid.get(driver_uid)


def latest_primary_reading() -> Optional[Dict[str, Any]]:
    if PRIMARY_DRIVER is not None:
        return latest_reading_for_driver(PRIMARY_DRIVER)

    with state_lock:
        return next(iter(latest_readings_by_uid.values()), None)


def latest_spn1_reading() -> Optional[Dict[str, Any]]:
    with state_lock:
        for driver in DRIVERS:
            if isinstance(driver, SPN1Driver):
                return latest_readings_by_uid.get(driver.uid)
    return None


def latest_readings() -> List[Dict[str, Any]]:
    with state_lock:
        return [
            latest_readings_by_uid[driver.uid]
            for driver in DRIVERS
            if getattr(driver, "uid", None) in latest_readings_by_uid
        ]


def configured_wifi_nodes(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = []
    for entry in config.get("drivers", []):
        if entry.get("driver") != "wifi_node":
            continue

        driver_config = entry.get("config", {})
        nodes.append(
            {
                "uid": entry.get("uid"),
                "driver": entry.get("driver"),
                "host": driver_config.get("host"),
                "port": driver_config.get("port", 1234),
            }
        )
    return nodes


def get_spn1_driver() -> SPN1Driver:
    for driver in DRIVERS:
        if isinstance(driver, SPN1Driver):
            return driver
    raise HTTPException(status_code=404, detail="SPN1 driver not configured")


@app.on_event("startup")
def start_background_reader() -> None:
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()

    sync_thread = threading.Thread(target=spn1_time_sync_loop, daemon=True)
    sync_thread.start()


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_CONFIG.get("title", "Solar Monitor"),
            "app_id": APP_CONFIG.get("app_id", "solar-monitor"),
            "poll_interval_ms": APP_CONFIG.get("poll_interval_ms", 1500),
            "configured_wifi_nodes": configured_wifi_nodes(APP_CONFIG),
        },
    )


@app.get("/time")
def get_time():
    now_utc = utc_now()
    now_local = local_now()
    status = time_status()
    return JSONResponse(
        {
            "utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local": now_local.strftime("%a %b %d, %H:%M:%S"),
            "local_tz": now_local.tzname(),
            "time_status": status,
        }
    )


@app.get("/app/info")
def get_app_info():
    return JSONResponse(
        {
            "app_id": APP_CONFIG.get("app_id", "solar-monitor"),
            "title": APP_CONFIG.get("title", "Solar Monitor"),
            "mode": APP_CONFIG.get("mode", "sensor_monitor"),
            "driver_count": len(DRIVERS),
        }
    )


@app.get("/app/health")
def get_app_health():
    return JSONResponse(
        {
            "ok": bool(DRIVERS),
            "status": "ok" if DRIVERS else "degraded",
            "time_status": time_status(),
            "driver_count": len(DRIVERS),
            "run_active": is_run_active(),
            "spn1_time_sync": get_spn1_sync_status(),
        }
    )


@app.get("/drivers")
def get_drivers():
    payload = []
    for driver in DRIVERS:
        payload.append(driver_payload(driver))
    return JSONResponse({"drivers": payload})


@app.get("/readings/latest")
def get_latest_readings():
    return JSONResponse({"readings": latest_readings()})


@app.get("/state")
def get_state():
    return JSONResponse(
        {
            "run_active": is_run_active(),
            "latest_reading": latest_primary_reading(),
            "latest_spn1_reading": latest_spn1_reading(),
            "latest_readings": latest_readings(),
            "spn1_time_sync": get_spn1_sync_status(),
        }
    )


@app.get("/spn1/status")
def get_spn1_status():
    driver = get_spn1_driver()
    return JSONResponse(driver.get_device_status())


@app.get("/spn1/time")
def get_spn1_time():
    driver = get_spn1_driver()
    return JSONResponse(driver.get_device_time())


@app.post("/spn1/time/sync")
def sync_spn1_time():
    get_spn1_driver()
    return JSONResponse(sync_spn1_time_once(reason="manual"))


@app.post("/start")
def start_run():
    global run_active
    with state_lock:
        run_active = True
    return JSONResponse({"run_active": True})


@app.post("/stop")
def stop_run():
    global run_active
    with state_lock:
        run_active = False
    return JSONResponse({"run_active": False})

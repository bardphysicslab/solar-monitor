import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from raspi.drivers.spn1_driver import SPN1Driver


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "app_config.example.json"

app = FastAPI(title="Solar Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

run_active = False
latest_reading: Optional[Dict[str, Any]] = None
state_lock = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return utc_now().astimezone()


def load_config() -> Dict[str, Any]:
    config_path = Path(os.environ.get("BARDBOX_APP_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


APP_CONFIG = load_config()


def load_drivers(config: Dict[str, Any]) -> List[Any]:
    loaded = []
    for entry in config.get("drivers", []):
        driver_name = entry.get("driver")
        uid = entry.get("uid", "spn1-0001")
        driver_config = entry.get("config", {})

        if driver_name == "spn1":
            loaded.append(
                SPN1Driver(
                    uid=uid,
                    port=driver_config.get("port", "/dev/cu.PL2303G-USBtoUART1130"),
                    baud=int(driver_config.get("baud", 9600)),
                )
            )
        else:
            raise ValueError(f"Unsupported driver in solar monitor: {driver_name}")
    return loaded


DRIVERS = load_drivers(APP_CONFIG)
PRIMARY_DRIVER = DRIVERS[0] if DRIVERS else None


def time_status() -> Dict[str, Any]:
    return {
        "valid": True,
        "source": "system",
        "sane": True,
        "ntp_synced": False,
    }


def set_latest_reading(reading: Dict[str, Any]) -> None:
    global latest_reading
    with state_lock:
        latest_reading = reading


def is_run_active() -> bool:
    with state_lock:
        return run_active


def polling_loop() -> None:
    while True:
        if is_run_active() and PRIMARY_DRIVER is not None:
            reading = PRIMARY_DRIVER.get_reading()
            if reading["status"] == "ok":
                set_latest_reading(reading)
            time.sleep(1.0)
        else:
            time.sleep(0.2)


def latest_readings() -> List[Dict[str, Any]]:
    with state_lock:
        return [latest_reading] if latest_reading is not None else []


def get_spn1_driver() -> SPN1Driver:
    for driver in DRIVERS:
        if isinstance(driver, SPN1Driver):
            return driver
    raise HTTPException(status_code=404, detail="SPN1 driver not configured")


@app.on_event("startup")
def start_background_reader() -> None:
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_CONFIG.get("title", "Solar Monitor"),
            "app_id": APP_CONFIG.get("app_id", "solar-monitor"),
            "poll_interval_ms": APP_CONFIG.get("poll_interval_ms", 1500),
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
            "ok": PRIMARY_DRIVER is not None,
            "status": "ok" if PRIMARY_DRIVER is not None else "degraded",
            "time_status": time_status(),
            "driver_count": len(DRIVERS),
            "run_active": is_run_active(),
        }
    )


@app.get("/drivers")
def get_drivers():
    payload = []
    for driver in DRIVERS:
        payload.append(
            {
                "info": driver.get_info(),
                "capabilities": driver.get_capabilities(),
            }
        )
    return JSONResponse({"drivers": payload})


@app.get("/readings/latest")
def get_latest_readings():
    return JSONResponse({"readings": latest_readings()})


@app.get("/state")
def get_state():
    driver_payload = None
    if PRIMARY_DRIVER is not None:
        driver_payload = {
            "info": PRIMARY_DRIVER.get_info(),
            "capabilities": PRIMARY_DRIVER.get_capabilities(),
        }

    with state_lock:
        return JSONResponse(
            {
                "run_active": run_active,
                "latest_reading": latest_reading,
                "driver": driver_payload,
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
    if is_run_active():
        raise HTTPException(status_code=409, detail="Stop run before syncing SPN1 time.")

    driver = get_spn1_driver()
    return JSONResponse(driver.sync_device_time(local_now()))


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

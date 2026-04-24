import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from raspi.drivers.example_driver import ExampleDriver


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "app_config.example.json"

app = FastAPI(title="Bard Box Project Template")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


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
        uid = entry.get("uid", "bb-0000")
        driver_config = entry.get("config", {})

        if driver_name == "example":
            loaded.append(ExampleDriver(uid=uid, config=driver_config))
        else:
            raise ValueError(f"Unsupported driver in template: {driver_name}")
    return loaded


DRIVERS = load_drivers(APP_CONFIG)


def time_status() -> Dict[str, Any]:
    return {
        "valid": True,
        "source": "system",
        "sane": True,
        "ntp_synced": False,
    }


def latest_readings() -> List[Dict[str, Any]]:
    return [driver.get_reading() for driver in DRIVERS]


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": APP_CONFIG.get("title", "Example Deployment Monitor"),
            "app_id": APP_CONFIG.get("app_id", "bb-example-monitor"),
            "poll_interval_ms": APP_CONFIG.get("poll_interval_ms", 1000),
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
            "app_id": APP_CONFIG.get("app_id", "bb-example-monitor"),
            "title": APP_CONFIG.get("title", "Example Deployment Monitor"),
            "mode": APP_CONFIG.get("mode", "sensor_monitor"),
            "driver_count": len(DRIVERS),
        }
    )


@app.get("/app/health")
def get_app_health():
    return JSONResponse(
        {
            "ok": True,
            "status": "ok",
            "time_status": time_status(),
            "driver_count": len(DRIVERS),
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


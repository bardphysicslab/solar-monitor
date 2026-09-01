from collections import deque
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

from raspi.drivers.et54_driver import ET54Driver
from raspi.drivers.spn1_driver import SPN1Driver
from raspi.drivers.wifi_node_driver import WiFiNodeDriver
from raspi.load_control import ElectronicLoadController, LoadControlError
from raspi.backup import DataBackupManager, backup_config_from_app_config
from raspi.recording.csv_recorder import CsvAveragingRecorder, recorder_configs_from_app_config
from raspi.recording.sweep_recorder import SweepRecorder
from raspi.data_api import create_data_api_router


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "app_config.json"
DEFAULT_RECORDING_DATA_ROOT = PROJECT_ROOT / "data" / "sensor_data"
DEFAULT_BACKUP_SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "backup_snapshots"
logger = logging.getLogger(__name__)

app = FastAPI(title="Solar Monitor")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

run_active = False
latest_readings_by_uid: Dict[str, Dict[str, Any]] = {}
last_recorded_signatures_by_uid: Dict[str, str] = {}
state_lock = threading.Lock()
sync_status_lock = threading.Lock()
shutdown_event = threading.Event()
spn1_sample_lock = threading.Lock()
spn1_irradiance_samples = deque(maxlen=10000)
sweep_threads: Dict[str, threading.Thread] = {}
spn1_sync_status: Dict[str, Any] = {
    "auto_sync_enabled": False,
    "sync_interval_hours": 24,
    "last_sync_attempt_utc": None,
    "last_sync_success_utc": None,
    "last_sync_error": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record_spn1_irradiance(reading: Dict[str, Any]) -> None:
    if reading.get("status") != "ok":
        return
    value = (reading.get("data") or {}).get("total_w_m2")
    if not isinstance(value, (int, float)):
        return
    timestamp = reading.get("timestamp") or utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    with spn1_sample_lock:
        spn1_irradiance_samples.append((timestamp, float(value)))


def irradiance_values_between(start: str, end: str) -> List[float]:
    with spn1_sample_lock:
        return [value for timestamp, value in spn1_irradiance_samples if start <= timestamp <= end]


def local_now() -> datetime:
    return utc_now().astimezone()


def load_config() -> Dict[str, Any]:
    config_path = Path(os.environ.get("BARDBOX_APP_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


APP_CONFIG = load_config()
app.include_router(create_data_api_router(DEFAULT_RECORDING_DATA_ROOT, APP_CONFIG))


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
        elif driver_name == "et54":
            port = driver_config.get("port")
            if not port:
                raise ValueError(f"ET54 driver {uid} requires config.port")
            loaded.append(
                ET54Driver(
                    uid=uid,
                    port=port,
                    baud=int(driver_config.get("baud", 9600)),
                    mode=driver_config.get("mode", "CR"),
                    resistance_ohm=float(driver_config.get("resistance_ohm", 100)),
                    timeout_s=float(driver_config.get("timeout_s", 1.0)),
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


def panel_config_by_uid(config: Dict[str, Any], panel_uid: str) -> Optional[Dict[str, Any]]:
    for entry in config.get("drivers", []):
        if entry.get("driver") == "wifi_node" and entry.get("uid") == panel_uid:
            return entry
    return None


def build_load_controllers(config: Dict[str, Any], drivers: List[Any], sweep_recorder: SweepRecorder) -> Dict[str, ElectronicLoadController]:
    entries = {entry.get("uid"): entry for entry in config.get("drivers", [])}
    controllers = {}
    for driver in drivers:
        if not isinstance(driver, ET54Driver):
            continue
        entry = entries.get(driver.uid) or {}
        driver_config = entry.get("config") or {}
        panel_uid = driver_config.get("panel_uid")
        controllers[driver.uid] = ElectronicLoadController(
            driver=driver,
            load_config=driver_config,
            panel_config=panel_config_by_uid(config, panel_uid) if panel_uid else None,
            irradiance_provider=irradiance_values_between,
            sweep_result_sink=sweep_recorder.record,
        )
    return controllers


DRIVERS = load_drivers(APP_CONFIG)
PRIMARY_DRIVER = DRIVERS[0] if DRIVERS else None
SWEEP_RECORDER = SweepRecorder(DEFAULT_RECORDING_DATA_ROOT)
LOAD_CONTROLLERS = build_load_controllers(APP_CONFIG, DRIVERS, SWEEP_RECORDER)
RECORDER = CsvAveragingRecorder(
    recorder_configs_from_app_config(APP_CONFIG),
    data_root=DEFAULT_RECORDING_DATA_ROOT,
)
BACKUP_MANAGER = DataBackupManager(
    backup_config_from_app_config(APP_CONFIG),
    data_root=DEFAULT_RECORDING_DATA_ROOT,
    snapshot_root=DEFAULT_BACKUP_SNAPSHOT_ROOT,
    recorder=RECORDER,
)


def get_configured_spn1_driver() -> Optional[SPN1Driver]:
    for driver in DRIVERS:
        if isinstance(driver, SPN1Driver):
            return driver
    return None


def polled_drivers() -> List[Any]:
    return [driver for driver in DRIVERS if not isinstance(driver, SPN1Driver)]


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


def spn1_time_sync_loop(stop_event: threading.Event = shutdown_event) -> None:
    driver = get_configured_spn1_driver()
    if driver is None:
        configure_initial_spn1_sync_status()
        return

    configure_initial_spn1_sync_status()

    if driver.auto_sync_time:
        sync_spn1_time_once(reason="startup")

    while not stop_event.wait(60):
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


def reading_signature(reading: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "timestamp": reading.get("timestamp"),
            "raw": reading.get("raw"),
        },
        sort_keys=True,
        default=str,
    )


def is_fresh_for_recording(uid: str, reading: Dict[str, Any]) -> bool:
    signature = reading_signature(reading)
    with state_lock:
        if last_recorded_signatures_by_uid.get(uid) == signature:
            return False
        last_recorded_signatures_by_uid[uid] = signature
    return True


def is_run_active() -> bool:
    with state_lock:
        return run_active


def poll_all_drivers_once() -> None:
    for driver in DRIVERS:
        poll_driver_once(driver)
    try:
        RECORDER.flush_due()
    except Exception as exc:
        logger.warning("CSV recording due-flush failed: %s", exc)


def poll_driver_once(driver: Any) -> None:
    driver_uid = getattr(driver, "uid", "unknown")
    try:
        controller = LOAD_CONTROLLERS.get(driver_uid)
        if controller is not None:
            reading = controller.poll_reading()
        else:
            reading = driver.get_reading()
        if isinstance(driver, SPN1Driver):
            record_spn1_irradiance(reading)
        set_latest_reading(driver_uid, reading)
        try:
            if is_fresh_for_recording(driver_uid, reading):
                RECORDER.add_reading(driver_uid, reading)
        except Exception as exc:
            logger.warning("CSV recording failed to accept sample for %s: %s", driver_uid, exc)
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


def spn1_acquisition_loop(stop_event: threading.Event = shutdown_event) -> None:
    driver = get_configured_spn1_driver()
    if driver is None:
        return

    while not stop_event.is_set():
        if is_run_active():
            poll_driver_once(driver)
        else:
            stop_event.wait(0.2)


def generic_polling_loop(stop_event: threading.Event = shutdown_event, poll_interval_s: float = 1.0) -> None:
    drivers = polled_drivers()
    while not stop_event.is_set():
        if is_run_active() and drivers:
            for driver in drivers:
                if stop_event.is_set():
                    break
                controller = LOAD_CONTROLLERS.get(getattr(driver, "uid", ""))
                if controller is not None and controller.state()["sweep_state"] == "running":
                    continue
                poll_driver_once(driver)
            stop_event.wait(poll_interval_s)
        else:
            stop_event.wait(0.2)


def recorder_flush_loop(stop_event: threading.Event = shutdown_event, flush_interval_s: float = 0.25) -> None:
    while not stop_event.wait(flush_interval_s):
        if not is_run_active():
            continue
        try:
            RECORDER.flush_due()
        except Exception as exc:
            logger.warning("CSV recording scheduled flush failed: %s", exc)


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
    loads_by_panel = {}
    for entry in config.get("drivers", []):
        if entry.get("driver") != "et54":
            continue
        driver_config = entry.get("config") or {}
        panel_uid = driver_config.get("panel_uid")
        if panel_uid:
            loads_by_panel[panel_uid] = {
                "uid": entry.get("uid"),
                "load_type": driver_config.get("load_type", "electronic_load"),
                "driver": entry.get("driver"),
            }
    for entry in config.get("drivers", []):
        if entry.get("driver") != "wifi_node":
            continue

        driver_config = entry.get("config", {})
        node = {
            "uid": entry.get("uid"),
            "driver": entry.get("driver"),
            "host": driver_config.get("host"),
            "port": driver_config.get("port", 1234),
        }
        load = loads_by_panel.get(entry.get("uid"))
        if load is not None:
            node["load"] = load
            node["source_location"] = "local"
        else:
            node["source_location"] = "network"
        nodes.append(node)
    return nodes


def get_spn1_driver() -> SPN1Driver:
    for driver in DRIVERS:
        if isinstance(driver, SPN1Driver):
            return driver
    raise HTTPException(status_code=404, detail="SPN1 driver not configured")


@app.on_event("startup")
def start_background_reader() -> None:
    shutdown_event.clear()

    spn1_thread = threading.Thread(target=spn1_acquisition_loop, daemon=True)
    spn1_thread.start()

    polling_thread = threading.Thread(target=generic_polling_loop, daemon=True)
    polling_thread.start()

    recorder_thread = threading.Thread(target=recorder_flush_loop, daemon=True)
    recorder_thread.start()

    sync_thread = threading.Thread(target=spn1_time_sync_loop, daemon=True)
    sync_thread.start()


@app.on_event("shutdown")
def flush_recorders_on_shutdown() -> None:
    shutdown_event.set()
    try:
        RECORDER.flush_all()
    except Exception as exc:
        logger.warning("CSV recording shutdown flush failed: %s", exc)
    for controller in LOAD_CONTROLLERS.values():
        try:
            controller.disable()
        except Exception as exc:
            logger.warning("Electronic load shutdown disable failed for %s: %s", controller.uid, exc)
    for driver in DRIVERS:
        if hasattr(driver, "close"):
            driver.close()


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
            "recording": RECORDER.status(),
            "backup": BACKUP_MANAGER.status(),
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
            "recording": RECORDER.status(),
            "backup": BACKUP_MANAGER.status(),
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


def get_load_controller(uid: str) -> ElectronicLoadController:
    controller = LOAD_CONTROLLERS.get(uid)
    if controller is None:
        raise HTTPException(status_code=404, detail="Electronic load not configured")
    return controller


def load_error_response(exc: Exception) -> JSONResponse:
    return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


@app.get("/loads")
def get_loads():
    return JSONResponse({"loads": [load_state_payload(controller) for controller in LOAD_CONTROLLERS.values()]})


def load_state_payload(controller: ElectronicLoadController) -> Dict[str, Any]:
    state = controller.state()
    with state_lock:
        reading = latest_readings_by_uid.get(controller.uid)
    channels = ("voltage_v", "current_a", "power_w", "load_resistance_ohm")
    live_reading = {channel: None for channel in channels}
    if reading is not None and reading.get("status") == "ok":
        data = reading.get("data") or {}
        live_reading = {channel: data.get(channel) for channel in channels}
    state["live_reading"] = live_reading
    return state


@app.post("/loads/{uid}/select")
async def select_load_mode(uid: str, request: Request):
    controller = get_load_controller(uid)
    payload = await request.json()
    try:
        return JSONResponse(controller.select_mode(payload.get("mode"), payload.get("resistance_ohm")))
    except (LoadControlError, ValueError) as exc:
        return load_error_response(exc)


@app.post("/loads/{uid}/step")
async def step_load_resistance(uid: str, request: Request):
    controller = get_load_controller(uid)
    payload = await request.json()
    try:
        return JSONResponse(controller.select_resistance_step(float(payload.get("step_ohm")), int(payload.get("direction"))))
    except (LoadControlError, ValueError, TypeError) as exc:
        return load_error_response(exc)


@app.post("/loads/{uid}/fixed/enable")
def enable_fixed_load(uid: str):
    try:
        return JSONResponse(get_load_controller(uid).enable_fixed())
    except Exception as exc:
        return load_error_response(exc)


@app.post("/loads/{uid}/fixed/apply")
async def apply_fixed_load(uid: str, request: Request):
    payload = await request.json()
    try:
        return JSONResponse(get_load_controller(uid).apply_fixed(payload.get("resistance_ohm")))
    except Exception as exc:
        return load_error_response(exc)


@app.post("/loads/{uid}/disable")
async def disable_load(uid: str, request: Request):
    payload = await request.json()
    try:
        return JSONResponse(get_load_controller(uid).disable(bool(payload.get("clear_fault", False))))
    except Exception as exc:
        return load_error_response(exc)


@app.post("/loads/{uid}/sweep/start")
def start_load_sweep(uid: str):
    controller = get_load_controller(uid)
    if controller.state()["sweep_run_active"]:
        return load_error_response(LoadControlError("Sweep already running"))
    try:
        controller.validate_sweep_configuration()
    except LoadControlError as exc:
        return load_error_response(exc)
    thread = threading.Thread(target=controller.run_sweep_sequence, daemon=True)
    sweep_threads[uid] = thread
    thread.start()
    return JSONResponse(controller.state(), status_code=202)


@app.post("/loads/{uid}/sweep/run-mode")
async def select_sweep_run_mode(uid: str, request: Request):
    payload = await request.json()
    try:
        return JSONResponse(get_load_controller(uid).select_sweep_run_mode(payload.get("run_mode")))
    except Exception as exc:
        return load_error_response(exc)


@app.post("/start")
def start_run():
    global run_active
    with state_lock:
        run_active = True
        last_recorded_signatures_by_uid.clear()
    RECORDER.start()
    return JSONResponse({"run_active": True})


@app.post("/stop")
def stop_run():
    global run_active
    for controller in LOAD_CONTROLLERS.values():
        try:
            controller.disable()
        except Exception as exc:
            logger.warning("Electronic load stop failed for %s: %s", controller.uid, exc)
    with state_lock:
        run_active = False
    RECORDER.stop()
    return JSONResponse({"run_active": False})

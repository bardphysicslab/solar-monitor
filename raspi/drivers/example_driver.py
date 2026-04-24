import math
import time
from datetime import datetime, timezone
from typing import Any, Dict


class ExampleDriver:
    def __init__(self, uid: str = "bb-0000", config: Dict[str, Any] | None = None):
        self.uid = uid
        self.config = config or {}
        self._started_at = time.time()

    def get_info(self) -> dict:
        return {
            "uid": self.uid,
            "source_type": "example_sensor",
            "transport": "simulated",
            "protocol": "example",
            "firmware": None,
        }

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "temp_c": {"label": "Temperature", "unit": "°C"},
                "rh_pct": {"label": "Relative Humidity", "unit": "%"},
                "press_pa": {"label": "Pressure", "unit": "Pa"},
            },
            "raw_available": False,
        }

    def get_reading(self) -> dict:
        elapsed = time.time() - self._started_at
        temp_c = 21.6 + math.sin(elapsed / 18.0) * 0.8
        rh_pct = 43.0 + math.sin(elapsed / 24.0) * 2.5
        press_pa = 100980 + math.sin(elapsed / 30.0) * 45

        return {
            "uid": self.uid,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "ok",
            "data": {
                "temp_c": round(temp_c, 2),
                "rh_pct": round(rh_pct, 2),
                "press_pa": int(round(press_pa)),
            },
            "extended": {
                "note": "Example driver reading",
            },
            "raw": None,
        }


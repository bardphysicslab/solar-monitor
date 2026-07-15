import csv
import json
import logging
import socket
from datetime import datetime, timezone
from io import StringIO
from typing import Any, Optional


logger = logging.getLogger(__name__)


class WiFiNodeDriverError(RuntimeError):
    pass


class WiFiNodeDriver:
    def __init__(self, uid: str, host: str, port: int = 1234, timeout_s: float = 3.0):
        self.uid = uid
        self.host = host
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self._last_connection_state = "unknown"

    def get_info(self) -> dict:
        info = {
            "uid": self.uid,
            "source_type": "solar_panel_dummy_node",
            "driver": "wifi_node",
            "transport": "wifi_tcp",
            "protocol": "BardBox commands",
            "firmware": None,
            "host": self.host,
            "port": self.port,
            "connection_state": self._last_connection_state,
        }

        try:
            response = self._send_command("INFO")
        except WiFiNodeDriverError as exc:
            info["connection_state"] = "error"
            info["error"] = str(exc)
            return info

        info["connection_state"] = "ok"
        info["raw_info"] = response
        info.update(self._parse_info_response(response))
        returned_uid = self._extract_uid(info)
        if returned_uid:
            self._validate_uid(returned_uid)
        return info

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "panel_voltage_v": {"label": "Panel voltage", "unit": "V"},
            },
            "features": {
                "live_readings": True,
                "wifi_tcp_transport": True,
                "panel_voltage": True,
                "bardbox_commands": ["PING", "INFO", "STATUS", "HEADER", "READ", "START", "STOP"],
            },
            "raw_available": True,
        }

    def get_reading(self) -> dict:
        header_raw = self._send_command("HEADER")
        reading_raw = self._send_command("READ")
        parsed = self._parse_csv_reading(header_raw, reading_raw)

        returned_uid = self._extract_uid(parsed)
        if returned_uid:
            self._validate_uid(returned_uid)
        else:
            info = self.get_info()
            returned_uid = self._extract_uid(info)
            if returned_uid:
                self._validate_uid(returned_uid)

        panel_voltage_v = parsed.pop("panel_voltage_v", None)

        data = {
            "panel_voltage_v": panel_voltage_v,
        }

        extended = {}
        if returned_uid:
            extended["node_uid"] = returned_uid

        for key in ("voltage_ok", "wifi", "rssi_dbm"):
            if key in parsed:
                extended[key] = parsed.pop(key)

        for key, value in parsed.items():
            if key not in ("uid",):
                extended[key] = value

        return {
            "uid": self.uid,
            "timestamp": self._utc_timestamp(),
            "status": "ok",
            "data": data,
            "extended": extended,
            "raw": {
                "header": header_raw,
                "read": reading_raw,
            },
        }

    def _send_command(self, command: str) -> str:
        payload = f"{command.strip()}\n".encode("ascii")

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as conn:
                conn.settimeout(self.timeout_s)
                conn.sendall(payload)
                chunks = []

                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break

        except socket.timeout as exc:
            self._last_connection_state = "timeout"
            logger.warning("Wi-Fi node command timed out: %s %s:%s", command, self.host, self.port)
            raise WiFiNodeDriverError(
                f"Timed out sending {command} to Wi-Fi node {self.host}:{self.port}"
            ) from exc
        except ConnectionRefusedError as exc:
            self._last_connection_state = "refused"
            logger.warning("Wi-Fi node connection refused: %s:%s", self.host, self.port)
            raise WiFiNodeDriverError(
                f"Connection refused by Wi-Fi node {self.host}:{self.port}"
            ) from exc
        except OSError as exc:
            self._last_connection_state = "error"
            logger.warning("Wi-Fi node connection failed: %s:%s %s", self.host, self.port, exc)
            raise WiFiNodeDriverError(
                f"Could not reach Wi-Fi node {self.host}:{self.port}: {exc}"
            ) from exc

        response = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not response:
            self._last_connection_state = "empty_response"
            raise WiFiNodeDriverError(f"Empty response to {command} from Wi-Fi node {self.host}:{self.port}")

        self._last_connection_state = "ok"
        logger.info("Wi-Fi node command succeeded: %s %s:%s", command, self.host, self.port)
        return response

    def _parse_csv_reading(self, header_raw: str, reading_raw: str) -> dict:
        header = self._parse_csv_line(header_raw, "HEADER")
        reading = self._parse_csv_line(reading_raw, "READ")

        if len(header) != len(reading):
            logger.warning(
                "Malformed Wi-Fi node CSV: %s header fields, %s reading fields",
                len(header),
                len(reading),
            )
            raise WiFiNodeDriverError(
                f"Malformed CSV from Wi-Fi node: HEADER has {len(header)} fields, READ has {len(reading)}"
            )

        return {
            key.strip(): self._convert_value(value.strip())
            for key, value in zip(header, reading)
            if key.strip()
        }

    def _parse_csv_line(self, text: str, label: str) -> list[str]:
        if not text or not text.strip():
            raise WiFiNodeDriverError(f"Empty {label} response from Wi-Fi node")

        try:
            rows = list(csv.reader(StringIO(text.strip())))
        except csv.Error as exc:
            logger.warning("Malformed Wi-Fi node %s CSV: %s", label, exc)
            raise WiFiNodeDriverError(f"Malformed {label} CSV from Wi-Fi node: {exc}") from exc

        if len(rows) != 1 or not rows[0]:
            logger.warning("Malformed Wi-Fi node %s CSV: %r", label, text)
            raise WiFiNodeDriverError(f"Malformed {label} CSV from Wi-Fi node")

        return rows[0]

    def _parse_info_response(self, response: str) -> dict:
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            return {
                str(key): self._convert_value(value) if isinstance(value, str) else value
                for key, value in parsed.items()
            }

        if "," in response and "=" not in response:
            try:
                values = self._parse_csv_line(response, "INFO")
            except WiFiNodeDriverError:
                return {}
            if values:
                return {"node_info": values}

        info = {}
        for token in response.replace(",", "\n").splitlines():
            separator = "=" if "=" in token else ":" if ":" in token else None
            if separator is None:
                continue
            key, value = token.split(separator, 1)
            info[key.strip()] = self._convert_value(value.strip())
        return info

    def _convert_value(self, value: str) -> Any:
        if value == "":
            return None

        normalized = value.strip()
        try:
            if normalized.lower().startswith(("0x", "-0x")):
                return int(normalized, 16)
            if any(char in normalized for char in (".", "e", "E")):
                return float(normalized)
            return int(normalized)
        except ValueError:
            return normalized

    def _extract_uid(self, values: dict) -> Optional[str]:
        for key in ("uid", "node_uid", "device_uid", "id"):
            value = values.get(key)
            if value:
                return str(value)
        return None

    def _validate_uid(self, returned_uid: str) -> None:
        if returned_uid == self.uid:
            return

        logger.warning(
            "Wi-Fi node UID mismatch: configured %s, returned %s",
            self.uid,
            returned_uid,
        )
        raise WiFiNodeDriverError(
            f"Wi-Fi node UID mismatch: configured {self.uid}, node returned {returned_uid}"
        )

    def _utc_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

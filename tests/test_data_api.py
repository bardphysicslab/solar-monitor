import asyncio
import gzip
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI

from raspi.data_api import create_data_api_router


ROOT = Path(__file__).resolve().parents[1]


class DataApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "readings"
        self.root.mkdir()
        self.token = "read-only-test-token"
        self.app = self.make_app({"data_api": {"token": self.token}})
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_app(self, config):
        app = FastAPI()
        app.include_router(create_data_api_router(self.root, config))
        return app

    def request(self, path, headers=None, raw_path=None, app=None):
        messages = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        encoded_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": raw_path or path.encode(),
            "query_string": b"",
            "headers": encoded_headers,
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        }
        asyncio.run((app or self.app)(scope, receive, send))
        start = next(message for message in messages if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
        return start["status"], response_headers, body

    def json_response(self, *args, **kwargs):
        status, headers, body = self.request(*args, **kwargs)
        return status, headers, json.loads(body)

    def write(self, relative_path, content=b"timestamp,value\n2026-08-19T00:00:00Z,1\n"):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_authentication_is_required(self):
        status, headers, _ = self.request("/api/data/files")
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"], "Bearer")

    def test_download_rejects_missing_and_invalid_tokens(self):
        self.write("node/readings.csv")
        missing_status, _, _ = self.request("/api/data/files/node/readings.csv")
        invalid_status, _, _ = self.request(
            "/api/data/files/node/readings.csv",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(missing_status, 401)
        self.assertEqual(invalid_status, 401)

    def test_valid_token_succeeds(self):
        status, headers, payload = self.json_response("/api/data/files", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"files": []})
        self.assertEqual(headers["cache-control"], "no-store")

    def test_missing_or_empty_token_fails_closed(self):
        for config in (
            {"data_api": {"token": ""}},
            {},
        ):
            with self.subTest(config=config):
                status, _, _ = self.request("/api/data/files", headers=self.headers, app=self.make_app(config))
                self.assertEqual(status, 503)

    def test_committed_example_is_disabled_and_contains_only_empty_placeholder(self):
        config = json.loads((ROOT / "raspi" / "config" / "app_config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["data_api"], {"token": ""})

    def test_recursive_listing_includes_nested_files_and_excludes_non_csv(self):
        first = self.write("bb-solar-pnl-001/2026-08-19.csv")
        nested = self.write("external/reference/reference-2026-08-19.csv")
        self.write("external/reference/devices.json", b"{}")

        status, _, payload = self.json_response("/api/data/files", headers=self.headers)

        self.assertEqual(status, 200)
        files = payload["files"]
        self.assertEqual([item["path"] for item in files], [first.relative_to(self.root).as_posix(), nested.relative_to(self.root).as_posix()])
        for item in files:
            self.assertIsInstance(item["size_bytes"], int)
            self.assertRegex(item["modified_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_csv_retrieval(self):
        content = b"timestamp,value\n2026-08-19T00:00:00Z,7\n"
        self.write("node/readings.csv", content)
        status, headers, body = self.request("/api/data/files/node/readings.csv", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, content)
        self.assertTrue(headers["content-type"].startswith("text/csv"))
        self.assertEqual(headers["cache-control"], "no-store")

    def test_csv_gz_retrieval(self):
        content = gzip.compress(b"timestamp,value\n2026-08-19T00:00:00Z,7\n")
        self.write("node/readings.csv.gz", content)
        status, headers, body = self.request("/api/data/files/node/readings.csv.gz", headers=self.headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, content)
        self.assertEqual(headers["content-type"], "application/gzip")
        self.assertEqual(headers["cache-control"], "no-store")

    def test_missing_file_returns_404(self):
        status, _, _ = self.request("/api/data/files/node/missing.csv", headers=self.headers)
        self.assertEqual(status, 404)

    def test_parent_traversal_is_rejected(self):
        outside = self.root.parent / "outside.csv"
        outside.write_text("private", encoding="utf-8")
        status, _, body = self.request("/api/data/files/../outside.csv", headers=self.headers)
        self.assertEqual(status, 403)
        self.assertNotEqual(body, b"private")

    def test_url_encoded_parent_traversal_is_rejected(self):
        outside = self.root.parent / "outside.csv"
        outside.write_text("private", encoding="utf-8")
        status, _, body = self.request(
            "/api/data/files/../outside.csv",
            raw_path=b"/api/data/files/%2e%2e%2foutside.csv",
            headers=self.headers,
        )
        self.assertEqual(status, 403)
        self.assertNotEqual(body, b"private")

    @unittest.skipIf(not hasattr(os, "symlink"), "symbolic links are unavailable")
    def test_symlink_outside_readings_is_rejected_and_not_listed(self):
        outside = self.root.parent / "outside.csv"
        outside.write_text("private", encoding="utf-8")
        link = self.root / "linked.csv"
        link.symlink_to(outside)
        _, _, listing = self.json_response("/api/data/files", headers=self.headers)
        self.assertNotIn("linked.csv", [item["path"] for item in listing["files"]])
        status, _, _ = self.request("/api/data/files/linked.csv", headers=self.headers)
        self.assertEqual(status, 403)

    def test_directory_and_non_csv_retrieval_are_rejected(self):
        (self.root / "looks.csv").mkdir()
        self.write("notes.txt", b"not data")
        directory_status, _, _ = self.request("/api/data/files/looks.csv", headers=self.headers)
        non_csv_status, _, _ = self.request("/api/data/files/notes.txt", headers=self.headers)
        self.assertEqual(directory_status, 400)
        self.assertEqual(non_csv_status, 400)


if __name__ == "__main__":
    unittest.main()

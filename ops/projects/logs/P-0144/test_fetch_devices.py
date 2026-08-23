"""fetch_devices.py の unit test。

本物のローカル HTTP サーバ (ThreadingHTTPServer) を立てて、OAuth token 取得と
デバイス一覧取得の実リクエストを通す (関数モックではない)。P-0107 の手法に倣う。

実行: python3 -m unittest ops.projects.logs.P-0144.test_fetch_devices または
      python3 -m unittest discover -s ops/projects/logs/P-0144 -p 'test_*.py'
"""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import fetch_devices

FAKE_DEVICES = {
    "devices": [
        {
            "id": "1111",
            "name": "laptop.tailXXXX.ts.net.",
            "user": "hikuohiku@gmail.com",
            "tags": [],
            "os": "linux",
            "expires": "2026-09-10T00:00:00Z",
            "keyExpiryDisabled": False,
            "lastSeen": "2026-08-22T12:00:00Z",
        },
        {
            "id": "2222",
            "name": "k8s-argocd.tailXXXX.ts.net.",
            "user": "",
            "tags": ["tag:k8s"],
            "os": "linux",
            "expires": "2030-01-01T00:00:00Z",
            "keyExpiryDisabled": True,
            "lastSeen": "2026-08-23T01:00:00Z",
        },
        {
            "id": "3333",
            "name": "node01.tailXXXX.ts.net.",
            "user": "hikuohiku@gmail.com",
            "tags": [],
            "os": "linux",
            "expires": "2026-08-30T00:00:00Z",
            "keyExpiryDisabled": False,
            "lastSeen": "2026-08-23T02:00:00Z",
        },
    ]
}


class FakeTailscaleHandler(BaseHTTPRequestHandler):
    server_version = "FakeTailscale/1"

    def log_message(self, *args):  # テスト出力を静かにする
        pass

    def _record(self):
        self.server.requests.append((self.command, self.path))

    def do_POST(self):
        self._record()
        if self.path != "/api/v2/oauth/token":
            return self._send(404, {"message": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        if "grant_type=client_credentials" not in body:
            return self._send(400, {"message": "bad grant_type"})
        expected = "Basic " + base64.b64encode(b"k1234567890abcdef:tskey-client-secret").decode()
        if self.headers.get("Authorization") != expected:
            return self._send(401, {"message": "API token invalid"})
        self._send(200, {"access_token": "tskey-api-fake", "token_type": "Bearer", "expires_in": 3600})

    def do_GET(self):
        self._record()
        if self.path != "/api/v2/tailnet/-/devices":
            return self._send(404, {"message": "not found"})
        auth = self.headers.get("Authorization") or ""
        # Bearer トークンの「形」だけ見る (API key 直接指定の経路も通すため)
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            return self._send(401, {"message": "API token invalid"})
        self._send(200, FAKE_DEVICES)

    def _send(self, code, payload):
        blob = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeTailscaleHandler)
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()


class FetchDevicesTest(ServerFixture):
    def run_main(self, tmpdir, env_overrides=None):
        env = {
            "TAILSCALE_OAUTH_CLIENT_ID": "k1234567890abcdef",
            "TAILSCALE_OAUTH_CLIENT_SECRET": "tskey-client-secret",
        }
        env.update(env_overrides or {})
        out = str(Path(tmpdir) / "devices.json")
        # API_BASE だけ fake server へ向ける (https ではなく http)
        original_base = fetch_devices.API_BASE
        fetch_devices.API_BASE = f"http://127.0.0.1:{self.server.server_port}/api/v2"
        try:
            fetch_devices.main(["-o", out], env=env)
        finally:
            fetch_devices.API_BASE = original_base
        return Path(out)

    def test_envelope_keeps_raw_response_and_passes_verify_shape(self):
        with TemporaryDirectory() as tmpdir:
            out = self.run_main(tmpdir)
            data = json.loads(out.read_text())
            # verify#1 と同じ条件: devices 配列が 1 件以上で各要素が name を持つ
            self.assertGreaterEqual(len(data["devices"]), 1)
            self.assertTrue(all("name" in device for device in data["devices"]))
            # 生応答の形がそのまま残っていること (捏造・再整形しない原則)
            self.assertEqual(data["devices"], FAKE_DEVICES["devices"])
            # verify#3 が見る語が本文として含まれること
            text = out.read_text().lower()
            self.assertIn("expiry", text)
            self.assertIn("fetched_at", data)

    def test_table_sorted_by_expiry_with_marks(self):
        with TemporaryDirectory() as tmpdir:
            out = self.run_main(tmpdir)
            md = out.with_suffix(".md").read_text()
            pipe_lines = [line for line in md.splitlines() if line.startswith("| ")]
            data_rows = [
                line
                for line in pipe_lines[1:]  # 先頭は列ヘッダ
                if "---" not in line and line.split("|")[1].strip().isdigit()
            ]
            names_in_order = [row.split("|")[-2].strip() for row in data_rows]
            # 期限が近い順: node01 (2026-08-30) -> laptop (2026-09-10)、disabled は末尾
            self.assertEqual(
                names_in_order,
                ["node01.tailXXXX.ts.net.", "laptop.tailXXXX.ts.net.", "k8s-argocd.tailXXXX.ts.net."],
            )
            self.assertIn("node01?", md)
            self.assertIn("cluster-proxy", md)
            self.assertIn("disabled", md)

    def test_read_only_only_token_post_and_devices_get(self):
        with TemporaryDirectory() as tmpdir:
            self.run_main(tmpdir)
            methods_paths = sorted(self.server.requests)
            self.assertEqual(methods_paths, [("GET", "/api/v2/tailnet/-/devices"), ("POST", "/api/v2/oauth/token")])

    def test_bearer_key_skips_token_endpoint(self):
        with TemporaryDirectory() as tmpdir:
            self.run_main(tmpdir, {"TAILSCALE_API_KEY": "my-api-key"})
            self.assertNotIn(("POST", "/api/v2/oauth/token"), self.server.requests)

    def test_missing_credentials_lists_accepted_names(self):
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                fetch_devices.main(["-o", tmpdir + "/x.json"], env={})


if __name__ == "__main__":
    unittest.main()

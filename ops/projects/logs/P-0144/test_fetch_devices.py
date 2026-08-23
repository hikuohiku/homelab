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


class IngestTest(unittest.TestCase):
    """復元モード (--from-md / --from-json) の検査。

    人間が外で実測して issue #56 に貼ったデータを原本として devices.json に
    復元する経路。捏造しない原則のため、「表に無い情報を補わない」「通信しない」
    「既存の実測フィールドを上書きしない」ことを機械的に確かめる。
    """

    def test_from_md_restores_rendered_table(self):
        md = fetch_devices.render_table(FAKE_DEVICES["devices"], "2026-08-23T05:00:00Z")
        with TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "pasted.md"
            src.write_text(md, encoding="utf-8")
            out = Path(tmpdir) / "devices.json"
            fetch_devices.main(["--from-md", str(src), "-o", str(out)], env={})
            data = json.loads(out.read_text(encoding="utf-8"))
            # verify#1 と同じ条件: 各要素が name を持つ
            self.assertTrue(data["devices"])
            self.assertTrue(all("name" in device for device in data["devices"]))
            # verify#3 が見る語が本文として含まれること
            text = out.read_text(encoding="utf-8").lower()
            self.assertIn("expiry", text)
            # 転写である旨が envelope に明記されていること (実測と混同させない)
            self.assertIn("transcri", json.dumps(data).lower())
            by_name = {device["name"]: device for device in data["devices"]}
            node01 = by_name["node01.tailXXXX.ts.net."]
            self.assertEqual(node01["expires"], "2026-08-30T00:00:00Z")
            self.assertFalse(node01["keyExpiryDisabled"])
            k8s = by_name["k8s-argocd.tailXXXX.ts.net."]
            self.assertTrue(k8s["keyExpiryDisabled"])
            self.assertEqual(k8s.get("tags"), ["tag:k8s"])
            laptop = by_name["laptop.tailXXXX.ts.net."]
            self.assertEqual(laptop.get("user"), "hikuohiku@gmail.com")
            # 復元した録から render_table を再生成しても同じ表になる (往復性)
            self.assertEqual(
                fetch_devices.render_table(data["devices"], "2026-08-23T05:00:00Z"), md
            )

    def test_from_md_never_touches_network(self):
        md = fetch_devices.render_table(FAKE_DEVICES["devices"], "t")
        with TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "pasted.md"
            src.write_text(md, encoding="utf-8")
            original_base = fetch_devices.API_BASE
            # 接続を試みれば必ず即座に失敗するポートへ向けておく
            fetch_devices.API_BASE = "http://127.0.0.1:1/api/v2"
            try:
                fetch_devices.main(["--from-md", str(src), "-o", tmpdir + "/d.json"], env={})
            finally:
                fetch_devices.API_BASE = original_base

    def test_from_md_tolerates_code_fence_and_unknown_cells(self):
        pasted = "\n".join(
            [
                "人間が実行しました。結果です:",
                "```",
                "| # | 期限 (expires) | 失効設定 | lastSeen | os | user/tags | 印 | name |",
                "|---|----------------|----------|----------|----|-----------|----|------|",
                "| 1 | (不明) | enabled | (不明) | (不明) | (不明) | - | mystery.tailXXXX.ts.net. |",
                "| 2 | 2026-10-01T00:00:00Z | disabled | 2026-08-23T00:00:00Z | ios | alice@example.com, tag:prod | autopilot cluster-proxy | phone |",
                "```",
            ]
        )
        with TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "pasted.md"
            src.write_text(pasted, encoding="utf-8")
            out = Path(tmpdir) / "devices.json"
            fetch_devices.main(
                ["--from-md", str(src), "-o", str(out), "--fetched-at", "2026-08-23T09:00:00Z"],
                env={},
            )
            data = json.loads(out.read_text(encoding="utf-8"))
            mystery, phone = data["devices"]
            # 捏造しない: (不明) セルはキーごと省略する
            self.assertEqual(sorted(mystery.keys()), ["keyExpiryDisabled", "name"])
            self.assertFalse(mystery["keyExpiryDisabled"])
            self.assertEqual(phone["expires"], "2026-10-01T00:00:00Z")
            self.assertTrue(phone["keyExpiryDisabled"])
            self.assertEqual(phone.get("user"), "alice@example.com")
            self.assertEqual(phone.get("tags"), ["tag:prod"])
            self.assertEqual(data["fetched_at"], "2026-08-23T09:00:00Z")

    def test_from_md_rejects_non_table_and_short_rows(self):
        with TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "bad.md"
            bad.write_text("表ではありません\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                fetch_devices.main(["--from-md", str(bad), "-o", tmpdir + "/x.json"], env={})
            short = Path(tmpdir) / "short.md"
            short.write_text("| 1 | 2026-09-01T00:00:00Z | enabled | x |\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                fetch_devices.main(["--from-md", str(short), "-o", tmpdir + "/y.json"], env={})

    def test_from_json_keeps_raw_response_verbatim(self):
        raw = {"devices": [{"name": "x.example.", "expires": "2026-09-01T00:00:00Z"}]}
        with TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "pasted.json"
            src.write_text(json.dumps(raw), encoding="utf-8")
            out = Path(tmpdir) / "devices.json"
            fetch_devices.main(["--from-json", str(src), "-o", str(out)], env={})
            data = json.loads(out.read_text(encoding="utf-8"))
            # 生応答を再整形・欠損させず保持する (捏造しない原則)
            self.assertEqual(data["devices"], raw["devices"])

    def test_from_json_envelope_passthrough_preserves_fields(self):
        envelope = {
            "schema": "p-0144.devices/1",
            "fetched_at": "2026-08-22T22:00:00Z",
            "source": "original run on dev machine",
            "tailnet": "hikuohiku@gmail.com",
            "notes": {"expires": "独自の語義"},
            "raw_response_keys": ["devices"],
            "devices": FAKE_DEVICES["devices"],
        }
        with TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "pasted.json"
            src.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
            out = Path(tmpdir) / "devices.json"
            fetch_devices.main(["--from-json", str(src), "-o", str(out)], env={})
            data = json.loads(out.read_text(encoding="utf-8"))
            # 既存の実測フィールドを上書きしない
            self.assertEqual(data["fetched_at"], "2026-08-22T22:00:00Z")
            self.assertEqual(data["tailnet"], "hikuohiku@gmail.com")
            self.assertEqual(data["notes"]["expires"], "独自の語義")
            self.assertEqual(data["devices"], FAKE_DEVICES["devices"])
            self.assertIn("--from-json", data["source"])

    def test_both_flags_rejected(self):
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                fetch_devices.main(
                    [
                        "--from-md", tmpdir + "/a.md",
                        "--from-json", tmpdir + "/b.json",
                        "-o", tmpdir + "/x.json",
                    ],
                    env={},
                )


if __name__ == "__main__":
    unittest.main()

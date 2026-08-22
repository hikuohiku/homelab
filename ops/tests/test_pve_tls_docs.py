"""pveproxy TLS 台本 (docs/pveproxy-tls.md) と検証器 (ops/tools/check_pve_tls.sh) の
約束を固定する (P-0103)。

リポジトリルートから `python3 -m unittest ops.tests.test_pve_tls_docs`
(CI は `unittest discover -s ops/tests -t .` でも同じ物を掴む)。

検証器の終了コード契約は実サーバを相手に確認する: テスト内で、埋め込みの自己署名
証明書 (SAN=127.0.0.1/localhost) を提示するローカル HTTPS サーバを立てる。
SAN を一致させてあるので失敗理由は「未知の CA」だけに限定され、
unknown authority -> exit 1 / 信頼できる CA なら exit 0、という反転を正確に固定できる。
openssl 単体コマンドが無い環境 (Job イメージ) でも動くように、fixture は PEM 文字列を
埋め込んである。この鍵と証明書はテスト専用に生成した使い捨てで、秘密ではない
(外に繋がらないローカルサーバでしか使わない)。
"""

import http.server
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops" / "tools" / "check_pve_tls.sh"
DOC = ROOT / "docs" / "pveproxy-tls.md"

# テスト用 fixture (2026-08-22, P-0103 worker が生成)。CN=pve-tls-test.invalid、
# SAN=DNS:localhost, DNS:pve-tls-test.invalid, IP:127.0.0.1、有効期限 2036-08-19。
CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDEDCCAfigAwIBAgIJQtAkVr+2/u6hMA0GCSqGSIb3DQEBCwUAMB8xHTAbBgNV
BAMTFHB2ZS10bHMtdGVzdC5pbnZhbGlkMB4XDTI2MDgyMjE4MjEzOFoXDTM2MDgx
OTE4MjEzOFowHzEdMBsGA1UEAxMUcHZlLXRscy10ZXN0LmludmFsaWQwggEiMA0G
CSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCxFaoovVm2KXD35v/nZyBfuSkY6w6b
cLnPjxujICdT7/bsp9cfOPGuC3CsUXfOICVaMFXCHEh4DGaf8kKq8U8016jERyUY
aKB8FhX/7wVqiqrXU+YC031jlIYNs6UAoage4T8RvEfGXjFG/QlzP8cEpnGu3Rq/
fIPmkO8rW7Wr5evhhTJRZBYQEAwhlwF6u0RxBNNAnlmbZ1bAKHeS4lR7tREHs7C+
5S/Twn3zqMJdunr2Fs+QLxklvxtMiAwMbalGkN9Bx1jE3+XUrk++03quqiSpEWpZ
I2MhVlMdmvjrkhdnQPe0pjHlqErEO45OpWxuaZ7Gf9merjmSrcZjyu0HAgMBAAGj
TzBNMAwGA1UdEwQFMAMBAf8wCwYDVR0PBAQDAgKkMDAGA1UdEQQpMCeCCWxvY2Fs
aG9zdIIUcHZlLXRscy10ZXN0LmludmFsaWSHBH8AAAEwDQYJKoZIhvcNAQELBQAD
ggEBAK7MVZk5yqDGaguKZmXoDmBQ0WE6TTISrBjXrSUZy9CNhnUDsmKaDCVhWSWK
a9tc4ktWlKY2CRL9Ixjq2A/VwpuMExa0bpJlWyXS5mVf7xaMvnFnhVUjKJyVypTh
k2i6HpeAxc6igNwIQuZcIzLgeX+4nTQjCpicIanbUoSH4ooXBQPl7BvcBehqzxh7
/nwaPPbSXmrinzyKGTqFoMBZ9ba9REzuBEi39xvnBBIjp49tGn4nYyjlxcIYztmK
qNm666vQ7DYX8zk3GlP//w57eYWDhkLk3vRxlqIDC9v1ot+L4fiQH3rY4eBcD+CI
WjgW2w20pkE2c0u6vBZmlDQeiLs=
-----END CERTIFICATE-----"""

KEY_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIEogIBAAKCAQEAsRWqKL1Ztilw9+b/52cgX7kpGOsOm3C5z48boyAnU+/27KfX
HzjxrgtwrFF3ziAlWjBVwhxIeAxmn/JCqvFPNNeoxEclGGigfBYV/+8Faoqq11Pm
AtN9Y5SGDbOlAKGoHuE/EbxHxl4xRv0Jcz/HBKZxrt0av3yD5pDvK1u1q+Xr4YUy
UWQWEBAMIZcBertEcQTTQJ5Zm2dWwCh3kuJUe7URB7OwvuUv08J986jCXbp69hbP
kC8ZJb8bTIgMDG2pRpDfQcdYxN/l1K5PvtN6rqokqRFqWSNjIVZTHZr465IXZ0D3
tKYx5ahKxDuOTqVsbmmexn/Znq45kq3GY8rtBwIDAQABAoIBABPc4Tw2mPR0y4YS
9AtJpvj1tOVloDrRNxZP8AKpHaAtn13GKbwH0Bv8joPVk4GcW3Q1Tbk7IOnOQkiY
jN/Nt1uvAPAbJs8JFU81zvvpHItpyGXktD0G6d6cjzjEOLLMbyYfj8p2evqGIDLd
OQ0jTzh3TvMVO+XUmprnENylKciBDuJCCDLiLyEf3wwvYJWQA8jtEGxHLlbM9FM5
HynlDWoSDUIW9890h/eVmQnMvKVaRIOCcZSIBwHg93pm4eTpF1c2coBeAKYA4L4W
RoG7jLCjmaSb0dQQ8RazBn79Xz2BGjPM6DGTHY/DmIrPazXVlq6N/aGsXPn2JAGj
bTSzR5kCgYEA1+7CSFTDC9UpPLAvKxdTYRPRtxIKxq/oKPKIEjsAua33YsDdQcfA
q2/WcBqdBf5wgacDVW1CIsX/JecqNgzmTOvmeClO0q0phF/FGUly0Z5jsbiU8TVA
2yRQp1YzE3rl5iBvayT4DonWNvpog8Hnk97GE1Cd1NoJiVymW7ecTTsCgYEA0fGL
oWoAbPvMXzv4Dn5oyBAr7Id20lZUjGrG7xOL9XMKNxCtNDNX8HLpfXvKs6DDafnl
KA3npnQWshnRcfzHL8Rv6AZhBCjc5/zVRaRa1HL+mEPFoC7lzKQt2jw20c2TBiqb
ONMgOIBzhCe25thapVQHjmXCThlDs4bymL3tEqUCgYAJCaY6DsonGaHjN2CSBiTo
QEMbzXaEAGLQK+6EDNIn818SVA4uraSjyEeKY6LE2HRvCMV78tm8yNq4BGfg4UNW
Xt1DlD8HVFYTg0qll0xl96ImkxowylDPm0KA7nuuygsLSbpAGskDGsPLg0mSkjAW
IVHbBxnECegyRp1z40h4dwKBgEo7s1iWx6HBguklpjdRwTEEbtOKrpv/BJRF6SN9
8N4QGEuSLPwpL24I48CH9gt/y7j8MGfySreTkrbU5Db+31tnhP4wyzsSS9IHl02x
qKA4LQef5xAVpRGv97qT0fprSxJCHHKCTIFOAgp3lHeZhquww585IfRFgKfJDpyt
g1c1AoGAZVd14nFHmbGP9LNB3SHcueBc39+p3NoG9rilqSoGNKVnj6kvEv2KDqie
SZLWrR5C77415IotmQf4V3cbr/5zDFT8o/FQUkrWI8vvG3HhKbca3jTrBlrvqXL/
3Nvzikb2AY/Uyh46828GN4F0YYqtmTPC6tyQT0RBz1E7aPswV18=
-----END RSA PRIVATE KEY-----"""


class _SilentHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *args):
        pass


class _QuietServer(http.server.ThreadingHTTPServer):
    # 検証失敗側の接続ではクライアントがハンドシェイク途中で切るため
    # BrokenPipeError が上がる。テスト出力を汚すだけで意味がないので握り潰す
    def handle_error(self, request, client_address):
        pass


class TestArtifactsExist(unittest.TestCase):
    """受入項目そのもの。ファイルの存在・実行可能ビット・台本の必須文言。"""

    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.is_file(), f"{SCRIPT} が無い")
        self.assertTrue(os.access(SCRIPT, os.X_OK), f"{SCRIPT} に実行可能ビットが無い")

    def test_doc_mentions_tailscale_cert_and_api_upload_path(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("tailscale cert", text)
        self.assertIn("certificates/custom", text)

    def test_doc_ties_checker_into_the_playbook(self):
        """台本は検証器を使う手順と apply 解禁条件を含んでいなければ台本として不完全。"""
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("check_pve_tls.sh", text)
        self.assertIn("proxmox_download_file", text)


@unittest.skipUnless(SCRIPT.exists(), "check_pve_tls.sh が無い")
class TestCheckerAgainstLocalTLSServer(unittest.TestCase):
    """終了コード契約を実サーバ相手に固定する。

    0 = 検証成功 / 1 = TLS 検証失敗 (unknown authority ほか) /
    2 = 接続不能など判定不能。「現在 fail・解消後に反転」の両側をここで守る。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        base = Path(cls.tmpdir.name)
        cert = base / "fixture.crt"
        key = base / "fixture.key"
        cert.write_text(CERT_PEM)
        key.write_text(KEY_PEM)

        server = _QuietServer(("127.0.0.1", 0), _SilentHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        cls.server = server
        cls.port = server.server_address[1]
        cls.cert_path = cert

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmpdir.cleanup()

    def run_checker(self, *extra_args):
        cmd = [str(SCRIPT), "--host", "127.0.0.1", "--port", str(self.port), *extra_args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_unknown_authority_is_exit_1(self):
        proc = self.run_checker()
        self.assertEqual(
            proc.returncode,
            1,
            f"stdout={proc.stdout}\nstderr={proc.stderr}",
        )
        self.assertIn("FAIL", proc.stdout + proc.stderr)

    def test_trusted_ca_is_exit_0(self):
        """同じサーバでも CA を信頼させれば 0 に反転する — 検証器が見ているのは
        「誰が署名したか」であり、接続の可否ではないことの対偶。"""
        proc = self.run_checker("--ca", str(self.cert_path))
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        self.assertIn("OK", proc.stdout)
        self.assertIn("notAfter", proc.stdout)

    def test_unreachable_target_is_exit_2(self):
        """「TLS が壊れている」(1) と「届かないので判定不能」(2) を混同しない。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        proc = self.run_checker("--port", str(closed_port))
        self.assertEqual(
            proc.returncode,
            2,
            f"stdout={proc.stdout}\nstderr={proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()

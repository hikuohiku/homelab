#!/usr/bin/env python3
"""全アプリの Service 到達性マトリクス (P-9034)。

critic 2026-08-24 の利用者所見 4 への直接応答: adguard が 242 回 CrashLoopBackOff で
tailnet DNS が全停しても、ArgoCD の health (Deployment / Job の失敗しか見ない) は沈黙し、
2 日間人間に届かなかった。この計器は「人間が実際に叩く入口 (Service / tailnet) が
応答するか」を clusterIP と MagicDNS の dual-path で常設する。

- clusterIP path: 各アプリの Service を `<svc>.<ns>.svc` (クラスタ内 Service DNS) で叩く
- tailnet path:   tailnet 公開 Service (adguard / syncthing-sync) を MagicDNS 名
  (`<hostname>.<tailnet>`、既定 `tailae6c2.ts.net`) で叩く
- 結果は ops/health/reachability.json に JSON で出力する (生成物。git 管理外)

probe kind (判定の考え方):

  http     「任意の HTTP 応答」があれば到達 (status は問わない。400/500 でも
            入口が応答している事実は同じ)。接続失敗・タイムアウトで fail
  tcp      TCP 接続が張れれば到達
  udp      接続型 UDP で送達し、ICMP unreachable が返らなければ到達。
            応答の無いサイレントプロトコルは「到達したが応答なし」として ok —
            「死んだ UDP が静かにパケットを捨てる」場合はこれでは見えない。
            ただし adguard の DNS 死は dns-udp (応答を要求する) が直接捕まえる
  dns-tcp  DNS クエリを送り、正当な DNS 応答 (rcode 問わず。NXDOMAIN/REFUSED も
            「サーバは生きている」) を受ければ到達。応答なし・接続失敗で fail —
            adguard の「DNS 死」を最も直接に検知する検査
  dns-udp  同上 (UDP)

判定ロジックは probe を差し替え可能にしてあり、テスト (ops/tests/test_reachability_probe.py)
と `--selftest` は network-free の fixture (ops/tests/fixtures/reachability/) だけで回る。

既知の死角 (伏せずに書き残る):
  - この計器が観測するのは「probe を実行した場所から見た」到達性。コンテキスト依存の
    例: autopilot-heart のゲートは NetworkPolicy が送信元を app=autopilot-core の Pod に
    限定する (apps/autopilot/heart-service.yaml) ため、runner 等の他 Pod からは正常時でも
    refused/drop になる。「心臓が落ちている」のか「自分が宛先外なだけ」かはこの計器だけでは
    区別できない。実行コンテキスト (hostname) を reachability.json の context に記録する
  - MagicDNS 名の解決は実行コンテキストの resolver に依存する。クラスタ内では CoreDNS が
    .ts.net を ts-nameserver-fixed (10.43.0.53) へ転送し、tailnet デバイスが無ければ
    NXDOMAIN になる。adguard の tailnet デバイス消滅 (= DNS 死) は実測でこの経路 (2026-08-24
    baseline 参照)。tailnet メンバー端末からなら直接 MagicDNS が引けるため結果が変わりうる
  - clusterIP 到達は NetworkPolicy や Service の selector の影響を受ける。adguard の
    CrashLoopBackOff 時は clusterIP 53/3000 が refused/timeout になる (2026-08-24 実測)

判定ロジックの固定テスト: ops/tests/test_reachability_probe.py
(`python3 -m pytest ops/tests/test_reachability_probe.py -q` と
`python3 -m unittest ops.tests.test_reachability_probe` の両方で通る書き方)。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REACHABILITY_JSON = ROOT / "ops" / "health" / "reachability.json"
FIXTURE_ADGUARD_DNS_DEAD = (
    ROOT / "ops" / "tests" / "fixtures" / "reachability" / "adguard-dns-dead.json"
)

DEFAULT_TAILNET_DOMAIN = "tailae6c2.ts.net"  # ops/state.json / apps/*/ingress.yaml 実測
DEFAULT_TIMEOUT_S = 3.0

# DNS クエリの定数。ID は P-9034 の識別子を使う (ログ照合の手がかり)
DNS_ID = 0x9034
DNS_FLAGS = 0x0100  # RD
DNS_QNAME = "example.com"  # adguard は block 設定次第で NXDOMAIN を返すが応答は応答
DNS_QTYPE = 1  # A
DNS_QCLASS = 1  # IN


def build_dns_query(name=DNS_QNAME, qid=DNS_ID, qtype=DNS_QTYPE, qclass=DNS_QCLASS):
    """最小の DNS クエリ (header + QNAME + type/class) を返す。"""
    header = struct.pack(">HHHHHH", qid, DNS_FLAGS, 1, 0, 0, 0)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    return header + qname + b"\x00" + struct.pack(">HH", qtype, qclass)


def parse_dns_response(data, qid=DNS_ID):
    """応答バイト列が「このクエリに対する正当な DNS 応答」か (ok, detail)。

    rcode は問わない (NXDOMAIN/REFUSED でもサーバは生きている = 到達)。見るのは
    ID 一致と QR ビットだけ。data がヘッダ (12 バイト) 未満なら fail。
    """
    if len(data) < 12:
        return False, "応答が短すぎる ({} バイト)".format(len(data))
    resp_id, flags, _qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if resp_id != qid:
        return False, "DNS ID 不一致 (期待 {:#x}, 受信 {:#x})".format(qid, resp_id)
    if not (flags & 0x8000):
        return False, "QR ビットが無い (応答でない)"
    rcode = flags & 0x000F
    return True, "DNS 応答 (rcode={}, answers={})".format(rcode, an)


def resolve_host(host, timeout):
    """host をシステムの resolver で引く。成功なら (True, 解決先 IP)。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        return True, infos[0][4][0]
    except socket.gaierror as e:
        return False, "名前解決失敗: {}".format(e)


def ok_resolver(host, timeout):
    """常に解決できる resolver。テスト / --selftest が network-free のため注入する。"""
    return True, "127.0.0.1"


def _elapsed_since(started):
    return int(round((time.monotonic() - started) * 1000))


def _probe_tcp(host, port, timeout):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "TCP 接続成功"
    except socket.timeout:
        return False, "接続タイムアウト ({}s)".format(timeout)
    except OSError as e:
        return False, "接続失敗: {}".format(e)


def _probe_udp(host, port, timeout):
    """接続型 UDP で送達確認。ICMP unreachable が返れば fail、それ以外は ok。

    応答の無いサイレントプロトコル (syncthing-sync の 22000/udp 等) は「到達したが
    応答なし」として ok にする — 応答を要求すると健康な相手まで fail になるため。
    DNS 死の検知は dns-udp (応答を要求する) が担う。
    """
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.send(b"\x00reachability-probe\x00")
        try:
            data, _addr = sock.recvfrom(65535)
            return True, "UDP 応答受信 ({} バイト)".format(len(data))
        except socket.timeout:
            return True, "UDP 送達成功 (応答なし, recv タイムアウト)"
    except OSError as e:
        return False, "UDP 送達失敗: {}".format(e)
    finally:
        sock.close()


def _recv_exact(sock, n):
    chunks = []
    while n > 0:
        chunk = sock.recv(n)
        if not chunk:
            raise OSError("接続が閉じられた (EOF)")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def _probe_dns_tcp(host, port, timeout):
    query = build_dns_query()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack(">H", len(query)) + query)
            (n,) = struct.unpack(">H", _recv_exact(sock, 2))
            data = _recv_exact(sock, n)
    except socket.timeout:
        return False, "DNS (tcp) 応答タイムアウト ({}s)".format(timeout)
    except OSError as e:
        return False, "DNS (tcp) 通信失敗: {}".format(e)
    ok, detail = parse_dns_response(data)
    return ok, "DNS (tcp) " + detail


def _probe_dns_udp(host, port, timeout):
    query = build_dns_query()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(query, (host, port))
        data, _addr = sock.recvfrom(4096)
    except socket.timeout:
        return False, "DNS (udp) 応答タイムアウト ({}s)".format(timeout)
    except OSError as e:
        return False, "DNS (udp) 通信失敗: {}".format(e)
    finally:
        sock.close()
    ok, detail = parse_dns_response(data)
    return ok, "DNS (udp) " + detail


def _probe_http(host, port, http_path, timeout):
    url = "http://{}:{}{}".format(host, port, http_path)
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "homelab-reachability-probe"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return True, "HTTP {}".format(resp.status)
    except urllib.error.HTTPError as e:
        # 応答コード付きで返った = 入口は生きている
        return True, "HTTP {} (応答あり)".format(e.code)
    except Exception as e:  # noqa: BLE001 — 到達できない理由を全部 detail に残す
        return False, "HTTP 到達失敗: {}: {}".format(type(e).__name__, e)


def real_probe(tid, kind, host, port, http_path, timeout):
    """本物のネットワーク層。tid は target_id (ログ用。ここでは使わない)。"""
    if kind == "tcp":
        return _probe_tcp(host, port, timeout)
    if kind == "udp":
        return _probe_udp(host, port, timeout)
    if kind == "http":
        return _probe_http(host, port, http_path, timeout)
    if kind == "dns-tcp":
        return _probe_dns_tcp(host, port, timeout)
    if kind == "dns-udp":
        return _probe_dns_udp(host, port, timeout)
    raise ValueError("未知の kind: {!r}".format(kind))


def target_id(t):
    """対象の安定 id。fixture のキーと一致させる (app:route:port:kind)。"""
    return "{}:{}:{}:{}".format(t["app"], t["route"], t["port"], t["kind"])


def build_targets(tailnet_domain):
    """対象 Service の一覧。apps/*/service*.yaml の実物から 2026-08-24 に読み取った。

    clusterIP path は `<svc>.<ns>.svc`、tailnet path は `<hostname>.<tailnet>`。
    """
    svc = lambda ns, name: "{}.{}.svc".format(name, ns)  # noqa: E731
    return [
        # --- clusterIP path ---
        {"app": "ops-dashboard", "route": "clusterip", "host": svc("autopilot", "ops-dashboard"),
         "port": 80, "kind": "http", "http_path": "/"},
        {"app": "coder", "route": "clusterip", "host": svc("coder", "coder"),
         "port": 80, "kind": "http", "http_path": "/"},
        {"app": "coder-postgres", "route": "clusterip", "host": svc("coder", "coder-postgres"),
         "port": 5432, "kind": "tcp"},
        {"app": "nats", "route": "clusterip", "host": svc("autopilot", "nats"),
         "port": 4222, "kind": "tcp"},
        {"app": "vaultwarden", "route": "clusterip", "host": svc("vaultwarden", "vaultwarden"),
         "port": 80, "kind": "http", "http_path": "/"},
        {"app": "syncthing", "route": "clusterip", "host": svc("syncthing", "syncthing"),
         "port": 8384, "kind": "http", "http_path": "/"},
        {"app": "immich-postgres", "route": "clusterip", "host": svc("immich", "immich-postgres"),
         "port": 5432, "kind": "tcp"},
        {"app": "autopilot-heart", "route": "clusterip", "host": svc("autopilot", "autopilot-heart"),
         "port": 8099, "kind": "http", "http_path": "/healthz"},
        # LoadBalancer (tailscale) にも clusterIP が割り当たる (2026-08-24 実測 10.43.23.45)
        {"app": "adguard", "route": "clusterip", "host": svc("adguard", "adguard"),
         "port": 53, "kind": "dns-tcp"},
        {"app": "adguard", "route": "clusterip", "host": svc("adguard", "adguard"),
         "port": 53, "kind": "dns-udp"},
        {"app": "adguard", "route": "clusterip", "host": svc("adguard", "adguard"),
         "port": 3000, "kind": "http", "http_path": "/"},
        # --- tailnet (MagicDNS) path ---
        {"app": "adguard", "route": "tailnet", "host": "adguard." + tailnet_domain,
         "port": 53, "kind": "dns-tcp"},
        {"app": "adguard", "route": "tailnet", "host": "adguard." + tailnet_domain,
         "port": 53, "kind": "dns-udp"},
        {"app": "adguard", "route": "tailnet", "host": "adguard." + tailnet_domain,
         "port": 3000, "kind": "http", "http_path": "/"},
        {"app": "syncthing-sync", "route": "tailnet", "host": "syncthing-sync." + tailnet_domain,
         "port": 22000, "kind": "tcp"},
        {"app": "syncthing-sync", "route": "tailnet", "host": "syncthing-sync." + tailnet_domain,
         "port": 22000, "kind": "udp"},
        {"app": "syncthing-sync", "route": "tailnet", "host": "syncthing-sync." + tailnet_domain,
         "port": 21027, "kind": "udp"},
    ]


def probe_target(target, probe, timeout, resolver=None):
    """target 1 件の結果 dict。名前解決は probe より先に判定する。

    state は 3 値:
      ok       名前解決も probe も成功 (到達)
      fail     名前解決は通ったが probe が失敗 (サービスの死を確認)
      unknown  名前解決自体が失敗 (この実行コンテキストからは観測不能。
               「死んだ DNS」かもしれないし、実行場所の resolver の制約かもしれない —
               in-cluster の runner 等からは健康な syncthing-sync の MagicDNS 名も
               NXDOMAIN になる実測がある。2026-08-24 baseline 参照)

    resolver は `(host, timeout) -> (ok, detail)`。差し替え可能で、テスト / --selftest は
    ネットワークに出ない resolver を注入する (既定はシステム resolver の resolve_host)。
    """
    started = time.monotonic()
    base = {
        "app": target["app"],
        "route": target["route"],
        "host": target["host"],
        "port": target["port"],
        "kind": target["kind"],
    }
    resolver = resolver or resolve_host
    resolved, resolve_detail = resolver(target["host"], timeout)
    if not resolved:
        return dict(
            base,
            state="unknown",
            resolve=False,
            resolve_detail=resolve_detail,
            ok=False,
            detail="名前解決失敗 (このコンテキストからは観測不能。DNS 死か resolver 制約かは判断しない)",
            elapsed_ms=_elapsed_since(started),
        )
    try:
        ok, detail = probe(
            target_id(target), target["kind"], target["host"],
            target["port"], target.get("http_path", "/"), timeout,
        )
    except Exception as e:  # noqa: BLE001 — 1 対象の失敗で全体を止めない
        ok, detail = False, "probe 実行エラー: {}: {}".format(type(e).__name__, e)
    return dict(
        base,
        state="ok" if ok else "fail",
        resolve=True,
        resolve_detail=resolved and resolve_detail,
        ok=ok,
        detail=detail,
        elapsed_ms=_elapsed_since(started),
    )


def run_probe(targets, probe, timeout, resolver=None):
    """全 target を順に観測する。順序は対象一覧の並びを保つ。"""
    return [probe_target(t, probe, timeout, resolver=resolver) for t in targets]


def summarize(results):
    """アプリ単位の集計。apps_fail = fail (確認された死) を含むアプリのみ。

    unknown (名前解決不能) は apps_fail に数えない — 健康な syncthing-sync の MagicDNS 名が
    in-cluster の resolver では NXDOMAIN になる実測があり、解決失敗は「死」の証明ではない
    (2026-08-24 baseline 参照)。確認できた死 (fail) と観測不能 (unknown) を分けて並べる。
    """
    by_app = {}
    for r in results:
        by_app.setdefault(r["app"], []).append(r)

    def app_state(app_rs):
        if any(r["state"] == "fail" for r in app_rs):
            return "fail"
        if any(r["state"] == "unknown" for r in app_rs):
            return "unknown"
        return "ok"

    states = {app: app_state(rs) for app, rs in by_app.items()}
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "fail": sum(1 for r in results if r["state"] == "fail"),
        "unknown": sum(1 for r in results if r["state"] == "unknown"),
        "apps_fail": sorted(a for a, s in states.items() if s == "fail"),
        "apps_unknown": sorted(a for a, s in states.items() if s == "unknown"),
        "apps_ok": sorted(a for a, s in states.items() if s == "ok"),
    }


def build_report(results, tailnet_domain, context=None):
    return {
        "schema": 1,
        "tool": "reachability_probe",
        "project": "P-9034",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tailnet_domain": tailnet_domain,
        "context": context or socket.gethostname(),
        "summary": summarize(results),
        "targets": results,
    }


def make_fixture_probe(fixture):
    """fixture ({"fail": {id: {"detail": ...}}, "default": {"detail": ...}}) から probe。

    指定 id は fail、それ以外は default の結果を返す。network-free の要 (テストと
    --selftest が共用する)。"""
    fail = fixture["fail"]
    default_detail = fixture.get("default", {}).get("detail", "ok")

    def probe(tid, kind, host, port, http_path, timeout):
        if tid in fail:
            return False, fail[tid].get("detail", "fail")
        return True, default_detail

    return probe


def run_selftest(tailnet_domain=DEFAULT_TAILNET_DOMAIN):
    """--selftest: fixture だけで判定ロジックを自己検証する (クラスタ実機不要)。

    戻り値 (ok: bool, lines: list[str])。
    """
    lines = ["--selftest: reachability_probe (P-9034) の自己検証 (network-free) --"]
    problems = []

    # 1. DNS codec の往復
    query = build_dns_query()
    lines.append("codec: クエリ {} バイト (QNAME={})".format(len(query), DNS_QNAME))
    resp = struct.pack(">HHHHHH", DNS_ID, 0x8180, 1, 0, 0, 0)
    ok, detail = parse_dns_response(resp)
    if not ok:
        problems.append("正常な DNS 応答を誤判定: {}".format(detail))
    lines.append("codec: 正常応答 -> {} ({})".format("ok" if ok else "NG", detail))
    ok2, _ = parse_dns_response(struct.pack(">HHHHHH", 0x0000, 0x8180, 1, 0, 0, 0))
    if ok2:
        problems.append("ID 不一致の応答を ok と誤判定")
    lines.append("codec: ID 不一致 -> {} (期待: NG)".format("ok" if ok2 else "NG"))
    ok3, _ = parse_dns_response(b"\x00")
    if ok3:
        problems.append("短すぎる応答を ok と誤判定")
    lines.append("codec: 短すぎる応答 -> {} (期待: NG)".format("ok" if ok3 else "NG"))

    # 2. adguard「DNS 死」シナリオ (fixture) で計器が捕まえること
    if not FIXTURE_ADGUARD_DNS_DEAD.exists():
        problems.append("fixture が無い: {}".format(FIXTURE_ADGUARD_DNS_DEAD))
    else:
        fixture = json.loads(FIXTURE_ADGUARD_DNS_DEAD.read_text(encoding="utf-8"))
        targets = build_targets(tailnet_domain)
        ids = {target_id(t) for t in targets}
        dead = set(fixture["fail"].keys())
        missing = dead - ids
        if missing:
            problems.append("fixture の fail id が実対象に無い: {}".format(sorted(missing)))
        results = run_probe(
            targets, make_fixture_probe(fixture), DEFAULT_TIMEOUT_S, resolver=ok_resolver)
        failed = [r for r in results if not r["ok"]]
        failed_ids = {target_id(r) for r in failed}
        if failed_ids != dead:
            problems.append(
                "判定ずれ: fail={}, 期待={}".format(sorted(failed_ids), sorted(dead)))
        summary = summarize(results)
        lines.append("scenario({}): fail={} / ok={} / total={}".format(
            fixture.get("scenario"), summary["fail"], summary["ok"], summary["total"]))
        for r in failed:
            lines.append("  caught: {} -> {}".format(target_id(r), r["detail"]))

        # 3. all-ok シナリオで偽陽性が出ないこと
        all_ok = {"fail": {}, "default": {"detail": "ok"}}
        ok_results = run_probe(
            targets, make_fixture_probe(all_ok), DEFAULT_TIMEOUT_S, resolver=ok_resolver)
        false_pos = [target_id(r) for r in ok_results if not r["ok"]]
        if false_pos:
            problems.append("all-ok シナリオで偽陽性: {}".format(false_pos))
        lines.append("scenario(all-ok): 偽陽性なし")

    if problems:
        lines.append("NG 項目:")
        for p in problems:
            lines.append("  - " + p)
        return False, lines
    lines.append("全部通った")
    return True, lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reachability_probe.py",
        description="全アプリの Service (clusterIP / tailnet) 到達性マトリクス (P-9034)")
    parser.add_argument("--out", default=str(REACHABILITY_JSON),
                        help="結果 JSON の書き先 (既定: ops/health/reachability.json)")
    parser.add_argument("--tailnet-domain",
                        default=os.environ.get("REACHABILITY_TAILNET_DOMAIN", DEFAULT_TAILNET_DOMAIN),
                        help="MagicDNS の tailnet ドメイン (既定: {})".format(DEFAULT_TAILNET_DOMAIN))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                        help="probe のタイムアウト秒 (既定 {})".format(DEFAULT_TIMEOUT_S))
    parser.add_argument("--no-write", action="store_true",
                        help="ファイルに書き出さず stdout のみ")
    parser.add_argument("--selftest", action="store_true",
                        help="fixture で自己検証 (network-free。クラスタ実機不要)")
    args = parser.parse_args(argv)

    if args.selftest:
        ok, lines = run_selftest(args.tailnet_domain)
        for line in lines:
            print(line)
        return 0 if ok else 1

    targets = build_targets(args.tailnet_domain)
    try:
        results = run_probe(targets, real_probe, args.timeout)
    except Exception as e:  # noqa: BLE001 — 装置自身の故障は rc=2 で「届かない」と区別する
        print("probe 実行に失敗 (tool/environment error): {}: {}".format(type(e).__name__, e),
              file=sys.stderr)
        return 2
    report = build_report(results, args.tailnet_domain)

    if args.no_write:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("結果を {} に書きました (ok={} / fail={})".format(
            out_path, report["summary"]["ok"], report["summary"]["fail"]))

    for r in results:
        mark = "[ ok  ]" if r["ok"] else "[FAIL ]"
        mark = "[unknown]" if r["state"] == "unknown" else mark
        print(" {} {}:{}:{} ({})".format(mark, r["app"], r["route"], r["port"], r["kind"]))
        print("       state={} detail={}".format(r["state"], r["detail"]))
    print("apps_fail={} / apps_unknown={}".format(
        report["summary"]["apps_fail"], report["summary"]["apps_unknown"]))
    # fail (確認された死) が 1 つでもあれば rc=1。unknown は「観測不能」であり死の証明ではない
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
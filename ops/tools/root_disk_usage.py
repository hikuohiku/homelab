#!/usr/bin/env python3
"""node01 ルートディスクの内訳実測と満杯予報 (P-9062)。

CPU 飽和 (P-9037) と違いディスク満杯は「静かに進行する」— pvc-usage-reporter は
PVC ごとの使用量しか見ず、ルートディスク全体が何に食われて「いつ満杯になるか」を
測る装置が無かった (T-0079: nodes[].allocatable/capacity の ephemeral-storage は
実ルートディスクと別物で約 252GiB 実容量に対し約 48.9GiB しか出ない)。本ツールは
その計器で、使用内訳の実測と日次増加量からの残り日数 (fill_days) 予報を出す。

取得源 (spec dod):
  - 総使用量 : kubelet stats/summary の `node.fs` (availableBytes/capacityBytes/
    usedBytes、nodes/proxy + nodes/stats の RBAC 追加が必要) か、pod 内の statvfs
    (`shutil.disk_usage("/")`) 実測。overlay の statvfs は下層のホストルートディスク
    の値を透過するため、pod 内からでも node01 の実使用量が読める (2026-08-25 実測:
    overlay 251.65GiB / used 74.07GiB / free 167.28GiB)。
  - 内訳      : イメージ (node.runtime.imageFs.usedBytes) と local-path PVC
    (node.pods[].volume[].fs.usedBytes の合計) は kubelet summary から取れる。
    k3s / containerd / ログは**非特権 pod からは hostPath 無しでは読めない**
    (/var/lib/rancher は pod 内に見えない、2026-08-25 実測)。読めない内訳は
    None =「計測不能」を正直に載せる (PROJECT.md 設計方針)。

fill 予報は履歴が要るが、latest.json は最新 1 点のみで上書きされる (report.py 設計)。
履歴は report.py が同一 ConfigMap の `root_disk_history.json` キーに保持する
(最小限に閉じる — PROJECT.md の「やらないこと」)。本ツールは履歴サンプル列を受け取り、
最小二乗で 1 日あたり増加量を当て、free_bytes / 増加量で残り日数を出す。

標準ライブラリのみで動く (report.py と同じく pip install 不要)。クラスタ到達は
ServiceAccount トークン (自動マウント) を使う。`--check` は同梱 fixture だけで
ネットワーク非依存に自己検査する (node_saturation.py --check と同じ思想)。

使い方:
  python3 ops/tools/root_disk_usage.py --check                # 自己検査 (ネットワーク非依存)
  python3 ops/tools/root_disk_usage.py --node node01 --json   # 実測して section を出力
  python3 ops/tools/root_disk_usage.py --summary s.json --history h.json \
      --json                                                  # オフライン (テスト用)

apps/ops-health-reporter/ に同一内容のコピーが置いてある (reporter の
configMapGenerator が /scripts に載せ、report.py から import される)。
drift は ops/check_root_disk_usage_script_sync.py (CI) が検出する —
直すときは必ず両方を同じ PR で直すこと。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.request

# 満杯予報を出すのに必要な観測窓 (日)。日次増加量は 1 日周期の変動 (夜間 backup 等) を
# 含むため、最低でも丸 1 日分の履歴が溜まるまでは予報しない (出し始めの誤差を避ける)。
MIN_WINDOW_DAYS = 1.0

# 履歴の保持件数。reporter は 30 分毎に走るので 96 件 ≈ 2 日分。ConfigMap の
# root_disk_history.json キー (1 件 ≈ 50B) は 96 件でも数 KB に収まり、1MB 上限に余裕
MAX_SAMPLES = 96

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
K8S_BASE = "https://{}:{}".format(K8S_HOST, K8S_PORT)

# 非特権 pod からは hostPath 無しでは読めない内訳 (2026-08-25 実測: /var/lib/rancher
# は pod 内に見えない)。計測不能を表す None を載せるための定数ではない —
# 内訳ディクショナリのキー名をここに揃えておく
BREAKDOWN_UNMEASURABLE = frozenset(("k3s_bytes", "containerd_bytes", "logs_bytes"))


def _num(v):
    """int へ変換。bool / None / 壊れた値は None (1 項目の壊れで計測全体を止めない)。"""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _usable_samples(samples):
    """履歴から予報に使えるサンプルだけを残す (壊れ耐性、P-9062)。

    `_read_root_disk_history` は ConfigMap の履歴を「list である」までしか検証しない
    ため、個別エントリが壊れていてもここで捨てる。ts が解釈できない、または
    used_bytes が数値でないサンプルは除外 — そうしないと daily_increase_bytes が
    KeyError 等を漏らし、collect() が root_disk 節全体を {"error": ...} にして
    fill_days キーの契約 (受入検証) を壊す。健全なサンプルだけから予報する。
    """
    out = []
    for s in (samples or []):
        if _epoch(s) is None:
            continue
        if _num(s.get("used_bytes")) is None:
            continue
        out.append(s)
    return out


def sample_from_summary(summary):
    """kubelet stats/summary から root_disk 計測サンプルを作る (純関数)。

    - 総使用量 : `node.fs` (usedBytes / capacityBytes / availableBytes)
    - イメージ : `node.runtime.imageFs.usedBytes`
    - local-path PVC 相当 : `node.pods[].volume[].fs.usedBytes` の合計 (kubelet summary
      は storage class を返さない。node01 は local-path が唯一の SC なので pod volume
      使用量の合計で近似し、フィールド名を local_path_pvc_bytes としている)

    node.fs が取れない (壊れている) 場合は None — 呼び出し側が statvfs へ倒す。
    内訳は取れるものだけ載せ、k3s/containerd/ログ は None (計測不能)。
    """
    if not isinstance(summary, dict):
        return None
    node = summary.get("node")
    if not isinstance(node, dict):
        return None
    fs = node.get("fs")
    if not isinstance(fs, dict):
        return None
    used = _num(fs.get("usedBytes"))
    cap = _num(fs.get("capacityBytes"))
    if used is None or cap is None:
        return None
    free = _num(fs.get("availableBytes"))
    if free is None:
        free = max(0, cap - used)

    images = None
    runtime = node.get("runtime")
    if isinstance(runtime, dict):
        image_fs = runtime.get("imageFs")
        if isinstance(image_fs, dict):
            images = _num(image_fs.get("usedBytes"))

    pvc = 0
    for pod in node.get("pods") or []:
        if not isinstance(pod, dict):
            continue
        for vol in pod.get("volume") or []:
            if not isinstance(vol, dict):
                continue
            v = _num((vol.get("fs") or {}).get("usedBytes"))
            if v is not None:
                pvc += v

    return {
        "source": "kubelet_summary",
        "capacity_bytes": cap,
        "used_bytes": used,
        "free_bytes": free,
        "images_bytes": images,
        "local_path_pvc_bytes": pvc,
        "k3s_bytes": None,
        "containerd_bytes": None,
        "logs_bytes": None,
    }


def sample_from_statvfs(total, used, free):
    """statvfs 実測 (pod 内 `shutil.disk_usage("/")` の透過値) からサンプルを作る。

    内訳 (イメージ / PVC / k3s / containerd / ログ) は非特権 pod からは読めないため
    全て None = 計測不能 (PROJECT.md の「読めない内訳は正直に載せる」)。
    """
    return {
        "source": "statvfs",
        "capacity_bytes": int(total),
        "used_bytes": int(used),
        "free_bytes": int(free),
        "images_bytes": None,
        "local_path_pvc_bytes": None,
        "k3s_bytes": None,
        "containerd_bytes": None,
        "logs_bytes": None,
    }


# ---------------------------------------------------------------------------
# k8s 到達 (report.py / node_saturation.py と同じ ServiceAccount トークン方式)
# ---------------------------------------------------------------------------

def k8s_get(path):
    req = urllib.request.Request(
        K8S_BASE + path, headers={"Authorization": "Bearer " + _sa_token()}
    )
    with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=15) as resp:
        return json.load(resp)


def _sa_token():
    with open(os.path.join(SA_DIR, "token")) as f:
        return f.read().strip()


def _ssl_ctx():
    return ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))


def fetch_kubelet_summary(node_name):
    """kubelet stats/summary を読む (nodes/proxy + nodes/stats の RBAC が要る)。

    取れないときは None (403 / トークン無し / タイムアウト / 応答が JSON でない
    (json.load の ValueError) のどれでも)。呼び出し側は None なら statvfs 実測へ
    倒す — 「summary が取れない」を一括で落とすため ValueError (JSONDecodeError /
    UnicodeDecodeError) も含める。取りこぼすと measure が例外を外へ漏らし
    root_disk 節全体が {"error": ...} になり fill_days の契約を壊す。
    """
    if not node_name:
        return None
    try:
        return k8s_get("/api/v1/nodes/{}/proxy/stats/summary".format(node_name))
    except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError):
        return None


def measure(node_name=None, statvfs_path="/", summary_doc=None):
    """ルートディスク計測を返す。kubelet summary → statvfs の順に試す。

    summary_doc はオフライン (テスト・fixture 注入) 用。summary 経路が取れるなら
    node.fs と内訳 (images / PVC) を、取れなければ statvfs の総量だけを載せる。
    """
    if summary_doc is None:
        summary_doc = fetch_kubelet_summary(node_name)
    if summary_doc is not None:
        s = sample_from_summary(summary_doc)
        if s is not None:
            return s
    total, used, free = shutil.disk_usage(statvfs_path)
    return sample_from_statvfs(total, used, free)


# ---------------------------------------------------------------------------
# 履歴と満杯予報 (純関数)
# ---------------------------------------------------------------------------

def _epoch(sample):
    """サンプルの ts ("YYYY-MM-DDTHH:MM:SSZ") を epoch 秒へ。壊れていれば None。"""
    ts = sample.get("ts") if isinstance(sample, dict) else None
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def append_sample(samples, used_bytes, now_iso, max_samples=MAX_SAMPLES):
    """履歴に今回のサンプルを追加し、最大件数で切り詰める (純関数)。

    同一 ts の再実行 (同じ分に 2 回走った等) は置き換える — 二重カウントで
    増加量が 0 に寄るのを防ぐ。
    """
    samples = list(samples or [])
    entry = {"ts": now_iso, "used_bytes": int(used_bytes)}
    if samples and samples[-1].get("ts") == now_iso:
        samples[-1] = entry
    else:
        samples.append(entry)
    return samples[-max_samples:]


def daily_increase_bytes(samples):
    """全サンプルに最小二乗線を当てた 1 日あたりの増加量 (bytes/day, float|None)。

    最古と最新の 2 点だけだと観測窓の両端ノイズに支配されるため全点でフィットする。
    None は「予報不能」— サンプルが 2 点未満 / 観測窓が MIN_WINDOW_DAYS 未満 /
    増加量が非正 (ディスクが増えていない・減っている)。壊れた ts / used_bytes の
    サンプルは除く (_usable_samples)。
    """
    samples = _usable_samples(samples)
    if len(samples) < 2:
        return None
    xs = [_epoch(s) / 86400.0 for s in samples]
    ys = [_num(s["used_bytes"]) for s in samples]
    span_days = xs[-1] - xs[0]
    if span_days <= 0 or span_days < MIN_WINDOW_DAYS:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    if slope <= 0:
        return None
    return slope


def forecast(samples, free_bytes):
    """満杯予報 (純関数)。fill_days は残り日数 (float) / None は予報不能。

    None の理由は note に人間向けに載せる (「計測不能」をデータとして正直に出す)。
    """
    rate = daily_increase_bytes(samples)
    note = None
    fill_days = None
    if rate is None:
        valid = _usable_samples(samples)
        raw_count = len(samples or [])
        if len(valid) < 2:
            dropped = raw_count - len(valid)
            if dropped > 0:
                # 履歴はあるが壊れたエントリ (ts/used_bytes 欠落・非数値) を捨てた結果
                # 予報不能。「履歴が 2 点に満たない」とだけ言うのは誤解を招く
                # (履歴が若いのではなく破損で失われている) ため、正直に件数を載せる
                note = (
                    "履歴 {} 件中 {} 件が壊れている (ts/used_bytes 欠落・非数値) "
                    "ため予報不能 — 健全なサンプルの蓄積が要る"
                ).format(raw_count, dropped)
            else:
                note = "履歴が 2 点に満たない (次の report 以降の蓄積が要る)"
        else:
            span_days = (_epoch(valid[-1]) - _epoch(valid[0])) / 86400.0
            if span_days < MIN_WINDOW_DAYS:
                note = (
                    "観測窓が {:.2f} 日未満 — 満杯予報には {} 日以上の履歴が要る"
                ).format(span_days, MIN_WINDOW_DAYS)
            else:
                note = "日次増加量が 0 以下 (使用量が増えていない/減っている)"
    elif free_bytes is None or free_bytes <= 0:
        note = "free_bytes が測れないため予報不能"
    else:
        fill_days = free_bytes / rate
    return {
        "daily_increase_bytes": int(rate) if rate is not None else None,
        "fill_days": round(fill_days, 1) if fill_days is not None else None,
        "samples": len(samples or []),
        "note": note,
    }


def build_section(sample, fc, node_name, now_iso):
    """latest.json の root_disk 節を組み立てる (純関数)。"""
    used_ratio = None
    if sample["capacity_bytes"]:
        used_ratio = round(sample["used_bytes"] / float(sample["capacity_bytes"]), 4)
    return {
        "node": node_name,
        "source": sample["source"],
        "capacity_bytes": sample["capacity_bytes"],
        "used_bytes": sample["used_bytes"],
        "free_bytes": sample["free_bytes"],
        "used_ratio": used_ratio,
        "breakdown": {
            "images_bytes": sample["images_bytes"],
            "local_path_pvc_bytes": sample["local_path_pvc_bytes"],
            "k3s_bytes": sample["k3s_bytes"],
            "containerd_bytes": sample["containerd_bytes"],
            "logs_bytes": sample["logs_bytes"],
        },
        "daily_increase_bytes": fc["daily_increase_bytes"],
        "fill_days": fc["fill_days"],
        "fill_days_note": fc["note"],
        "samples": fc["samples"],
        "checked_at": now_iso,
    }


def build_report(previous_samples, now_iso, node_name="node01", statvfs_path="/", summary_doc=None):
    """実測 → 履歴更新 → 予報 → section を返す (report.py が呼ぶ)。

    戻り値は (section, 更新後のサンプル列)。呼び出し側 (report.py) は新しい
    サンプル列を ConfigMap の root_disk_history.json キーへ永続化する。
    """
    sample = measure(
        node_name=node_name, statvfs_path=statvfs_path, summary_doc=summary_doc
    )
    samples = append_sample(previous_samples, sample["used_bytes"], now_iso)
    fc = forecast(samples, sample["free_bytes"])
    return build_section(sample, fc, node_name, now_iso), samples


# ---------------------------------------------------------------------------
# --check 自己検査 (ネットワーク非依存。fixture と引数検証のみ)
# ---------------------------------------------------------------------------

def _selfcheck():
    """同梱 fixture でロジックを固定する。失敗したら例外を投げる。"""
    def expect(cond, message):
        if not cond:
            raise AssertionError(message)

    # --- sample_from_summary: 実態に即した fixture ---
    summary = {
        "node": {
            "nodeName": "node01",
            "fs": {
                "availableBytes": 179000000000,
                "capacityBytes": 270000000000,
                "usedBytes": 74000000000,
            },
            "runtime": {
                "imageFs": {
                    "availableBytes": 100000000000,
                    "capacityBytes": 270000000000,
                    "usedBytes": 45000000000,
                }
            },
            "pods": [
                {
                    "podRef": {"name": "p1"},
                    "volume": [{"name": "data", "fs": {"usedBytes": 1000000000}}],
                },
                {
                    "podRef": {"name": "p2"},
                    "volume": [{"name": "home", "fs": {"usedBytes": 250000000}}],
                },
            ],
        }
    }
    s = sample_from_summary(summary)
    expect(s is not None, "summary から sample が取れない")
    expect(s["source"] == "kubelet_summary", "source が kubelet_summary でない")
    expect(s["used_bytes"] == 74000000000, "node.fs.usedBytes が取れていない")
    expect(s["capacity_bytes"] == 270000000000, "node.fs.capacityBytes が取れていない")
    expect(s["free_bytes"] == 179000000000, "node.fs.availableBytes が取れていない")
    expect(s["images_bytes"] == 45000000000, "imageFs.usedBytes が取れていない")
    expect(s["local_path_pvc_bytes"] == 1250000000, "pod volume 合計 (1e9+2.5e8) が違う")
    expect(s["k3s_bytes"] is None and s["containerd_bytes"] is None and s["logs_bytes"] is None,
           "非特権 pod から読めない内訳は None のはず")

    # summary が壊れている / 欠けている → None (statvfs へ倒す)
    expect(sample_from_summary(None) is None, "None は None")
    expect(sample_from_summary({}) is None, "空 dict は None")
    expect(sample_from_summary({"node": {}}) is None, "node に fs が無いと None")
    expect(sample_from_summary({"node": {"fs": {}}}) is None, "fs が空だと None")

    # availableBytes が無いとき capacity - used で補完
    s2 = sample_from_summary({"node": {"fs": {"usedBytes": 100, "capacityBytes": 200}}})
    expect(s2 is not None and s2["free_bytes"] == 100, "availableBytes 欠落は補完")

    # --- sample_from_statvfs ---
    sv = sample_from_statvfs(1000, 300, 700)
    expect(sv["source"] == "statvfs", "source が statvfs でない")
    expect(sv["used_bytes"] == 300 and sv["free_bytes"] == 700, "statvfs の値が違う")
    expect(sv["images_bytes"] is None and sv["local_path_pvc_bytes"] is None,
           "statvfs 経路の内訳は計測不能 (None)")

    # --- append_sample ---
    samples = append_sample([], 100, "2026-08-25T00:00:00Z")
    expect(len(samples) == 1 and samples[0]["used_bytes"] == 100, "初回追加")
    samples = append_sample(samples, 200, "2026-08-25T00:30:00Z")
    expect(len(samples) == 2 and samples[-1]["used_bytes"] == 200, "2 点目追加")
    samples = append_sample(samples, 210, "2026-08-25T00:30:00Z")
    expect(len(samples) == 2 and samples[-1]["used_bytes"] == 210,
           "同一 ts の再実行は置き換え (二重カウントしない)")
    many = []
    for i in range(MAX_SAMPLES + 5):
        many = append_sample(
            many, i, "2026-08-23T00:{:02d}:{:02d}Z".format(i // 60, i % 60)
        )
    expect(len(many) == MAX_SAMPLES, "履歴は MAX_SAMPLES に切り詰める")

    # --- daily_increase_bytes / forecast: 1 日 1 GiB で増える fixture ---
    gib = 1024 * 1024 * 1024
    hist = [
        {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100000000000},
        {"ts": "2026-08-24T00:00:00Z", "used_bytes": 100000000000 + gib},
        {"ts": "2026-08-25T00:00:00Z", "used_bytes": 100000000000 + 2 * gib},
    ]
    rate = daily_increase_bytes(hist)
    expect(rate is not None, "2 日窓で rate が取れない")
    expect(abs(rate - gib) < 1e3, "rate が 1 GiB/day にならない: {}".format(rate))
    fc = forecast(hist, 167000000000)
    expect(fc["daily_increase_bytes"] == gib, "daily_increase_bytes が 1 GiB でない")
    expect(fc["fill_days"] is not None and fc["fill_days"] > 100,
           "fill_days が 167GiB/1GiB/day ≈ 155 日にならない: {}".format(fc["fill_days"]))
    expect(fc["note"] is None, "予報が取れたのに note が要らない")

    # --- forecast: 予報不能のケースを正直に None + note ---
    fc0 = forecast([], 1000)
    expect(fc0["fill_days"] is None and fc0["daily_increase_bytes"] is None,
           "履歴 0 点は予報不能")
    expect(fc0["note"] is not None, "履歴 0 点の note が要る")

    short = [
        {"ts": "2026-08-25T00:00:00Z", "used_bytes": 100},
        {"ts": "2026-08-25T00:30:00Z", "used_bytes": 200},
    ]
    fc_short = forecast(short, 100000)
    expect(fc_short["fill_days"] is None, "観測窓 30 分は予報不能")
    expect("観測窓" in fc_short["note"], "観測窓不足の note が要る")

    # 履歴はあるが壊れたエントリを捨てて 2 点未満 → note は破損件数を正直に載せる
    corrupt_hist = [
        {"ts": "2026-08-23T00:00:00Z", "used_bytes": 100},
        {"ts": "2026-08-24T00:00:00Z"},  # used_bytes 欠落
        {"ts": "2026-08-25T00:00:00Z", "used_bytes": "abc"},  # 非数値
    ]
    fc_corrupt = forecast(corrupt_hist, 100000)
    expect(fc_corrupt["fill_days"] is None, "破損で 2 点未満は予報不能")
    expect("2 件が壊れている" in fc_corrupt["note"], "破損件数の note が要る")

    shrinking = [
        {"ts": "2026-08-23T00:00:00Z", "used_bytes": 200},
        {"ts": "2026-08-24T00:00:00Z", "used_bytes": 180},
        {"ts": "2026-08-25T00:00:00Z", "used_bytes": 160},
    ]
    fc_shrink = forecast(shrinking, 1000)
    expect(fc_shrink["fill_days"] is None, "減少傾向は予報不能")
    expect("0 以下" in fc_shrink["note"], "減少の note が要る")

    # --- build_report: summary 注入 (オフライン) で section と履歴更新が返る ---
    section, new_samples = build_report(
        [], "2026-08-25T00:00:00Z", node_name="node01", summary_doc=summary
    )
    expect(section["node"] == "node01" and section["source"] == "kubelet_summary",
           "build_report の section が要る")
    expect(section["used_ratio"] == round(74000000000 / 270000000000, 4),
           "used_ratio が違う")
    expect("fill_days" in section, "section に fill_days キーが要る")
    expect(len(new_samples) == 1 and new_samples[0]["used_bytes"] == 74000000000,
           "build_report が履歴を返す")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="root_disk_usage.py",
        description="node01 ルートディスクの内訳実測と満杯予報 (P-9062)。",
    )
    parser.add_argument("--check", action="store_true", help="自己検査 (ネットワーク非依存)")
    parser.add_argument("--node", default="node01", help="読み取る node 名 (既定: node01)")
    parser.add_argument("--summary", help="kubelet stats/summary の JSON ファイルを注入 (オフライン)")
    parser.add_argument("--history", help="履歴 samples の JSON ファイルを注入 (オフライン)")
    parser.add_argument("--statvfs-path", default="/", help="statvfs 実測の代替パス (テスト用)")
    parser.add_argument("--json", action="store_true", help="結果 (section) を JSON で出力")
    args = parser.parse_args(argv)

    if args.check:
        try:
            _selfcheck()
        except AssertionError as e:
            print("root_disk_usage --check FAILED: {}".format(e), file=sys.stderr)
            return 1
        print("root_disk_usage --check ok")
        return 0

    summary_doc = None
    if args.summary:
        with open(args.summary) as f:
            summary_doc = json.load(f)
    previous = []
    if args.history:
        with open(args.history) as f:
            previous = json.load(f)
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        section, _samples = build_report(
            previous,
            now_iso,
            node_name=args.node,
            statvfs_path=args.statvfs_path,
            summary_doc=summary_doc,
        )
    except Exception as e:  # noqa: BLE001 — 観測失敗は失敗として報告
        print("root_disk_usage: 観測失敗: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(section, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "node={} source={} capacity_bytes={} used_bytes={} free_bytes={} "
            "used_ratio={} images_bytes={} pvc_bytes={} fill_days={} "
            "daily_increase_bytes={} samples={} note={}".format(
                section["node"],
                section["source"],
                section["capacity_bytes"],
                section["used_bytes"],
                section["free_bytes"],
                section["used_ratio"],
                section["breakdown"]["images_bytes"],
                section["breakdown"]["local_path_pvc_bytes"],
                section["fill_days"],
                section["daily_increase_bytes"],
                section["samples"],
                section["fill_days_note"],
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
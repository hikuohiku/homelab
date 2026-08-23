"""argocd application-controller のメモリ実使用と limit の近接警報 (P-0181)。

2026-08-23、argocd-application-controller-0 は旧 limit で 4 回 OOMKilled した
(exit_code 137, restarts 4)。pod_issues は「死んだ後」しか語らず、limit の
何割まで迫っているかを常に言える計器が無かった。このモジュールはその判定だけを
担う純関数群で、クラスタやネットワークに触れない (download_budget.py と同じ分離)。

- 集約側 (apps/ops-health-reporter/report.py): pod_metrics (metrics-server) から
  usage を、実機 pod GET から spec.containers[].resources.limits.memory を取り、
  build_report() へ渡して latest.json / history jsonl の `argocd` キーへ載せる。
  limit をハードコードしないのが要点 — values.yaml を引き直しても reporter 側の
  追従作業が発生しない
- 閾値 N (usage が limit の何% 以上で warn 判定するか) は ops/rules.json の
  argocd_controller.memory_limit_warn_percent が正。reporter は in-cluster で
  rules.json を読めず、configMapGenerator も kustomization.yaml 外のファイルを
  読めないため apps/ops-health-reporter/argocd-alerts.json に同期コピーしており、
  drift は ops/check_argocd_alert_sync.py (CI) が落とす

quantity パース (parse_quantity_bytes) は ops/tools/argocd_memory_series.py と同じ
契約 (無印バイト / Ki / Mi / Gi の整数のみ、未知の揺れは黙って読まない)。両者は
別々の場所で単独稼働するため共通化せず重複を認めている (片方を変えたらもう片方も
変える。テストが同じ fixture 表で両方向に固定している)。

判定ロジックの固定テストは ops/tests/test_argocd_memory.py
(`python3 -m unittest ops.tests.test_argocd_memory`)。
report.py と同じく標準ライブラリのみで動く。import 副作用を持たないので
cluster 外の unit test から importlib で直接ロードできる。
"""

import re

# 観測対象。StatefulSet argocd-application-controller の固定名決め打ち
# (ops/tools/argocd_memory_series.py と同じ前提。StatefulSet 名を変えたら両方変える)
ARGOCD_NAMESPACE = "argocd"
CONTROLLER_POD = "argocd-application-controller-0"
CONTROLLER_CONTAINER = "application-controller"

_QUANTITY_RE = re.compile(r"^(\d+)(Ki|Mi|Gi)?$")
_SUFFIX_BYTES = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}


def parse_quantity_bytes(q):
    """Kubernetes quantity 文字列をバイト数 int へ。"320908Ki" -> 328609792。

    対応は無印 (バイト) / Ki / Mi / Gi の整数のみ。小数・m 接尾・未知の揺れは
    ValueError / TypeError (黙って読み飛ばさず、呼び出し側に数えさせる)。
    """
    if not isinstance(q, str):
        raise TypeError("quantity は文字列: {!r}".format(q))
    m = _QUANTITY_RE.match(q.strip())
    if not m:
        raise ValueError("未対応の quantity: {!r}".format(q))
    return int(m.group(1)) * _SUFFIX_BYTES[m.group(2) or ""]


def coerce_warn_percent(value):
    """閾値 N (0 < N <= 100 の int) の検査。壊れていたら ValueError。

    同期コピー (argocd-alerts.json) の破損・欠損は reporter 側の設定事故なので、
    沈黙して no_data に畳まずここで落とす (collect() が error エントリとして
    latest.json に見せる)。bool は int の派生なので明示的に弾く。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "memory_limit_warn_percent は int であること: {!r}".format(value)
        )
    if not 0 < value <= 100:
        raise ValueError(
            "memory_limit_warn_percent は 0 < N <= 100 の範囲であること: {!r}".format(
                value
            )
        )
    return value


def _fmt_mib(b):
    """人間用の粗い表現 (reason 文面向け)。機械読み取り用のフィールドは生 bytes。"""
    return "{:.1f}Mi".format(b / 1048576.0)


def judge(usage_bytes, limit_bytes, warn_percent):
    """usage と limit から近接度を判定する。純関数。

    status:
      no_data       usage が取れない (metrics-server にサンプルが無い等)
      unconfigured  limit が取れない (pod spec 読取失敗・limit 未設定等)。
                    比較軸が無い以上、決め打ちで鳴らしたり沈黙させたりしない
      ok            warn_percent 未満
      warn          warn_percent 以上・100% 未満 (境界含む、鳴る側に倒す)
      exceed        100% 以上 — OOMKill はこの直後の世界にある
    """
    if usage_bytes is None:
        return {
            "status": "no_data",
            "limit_usage_percent": None,
            "reason": "metrics-server 系列に使用量サンプルが無く、判定できない",
        }
    if not limit_bytes:
        # None または 0 (0 は比較軸として無効)
        return {
            "status": "unconfigured",
            "limit_usage_percent": None,
            "reason": "実機 pod の resources.limits.memory が読めず、比較軸が無い",
        }
    percent = round(usage_bytes * 100.0 / limit_bytes, 1)
    base = "実使用 {} / limit {} (= {}%)".format(
        _fmt_mib(usage_bytes), _fmt_mib(limit_bytes), percent
    )
    if percent >= 100.0:
        return {
            "status": "exceed",
            "limit_usage_percent": percent,
            "reason": base + "。limit 到達 — OOMKill の再発に直結する",
        }
    if percent >= warn_percent:
        return {
            "status": "warn",
            "limit_usage_percent": percent,
            "reason": base + "。閾値 ({}%) 近傍 — 新規の負荷投入は控えること".format(warn_percent),
        }
    return {
        "status": "ok",
        "limit_usage_percent": percent,
        "reason": base + "。閾値 ({}%) 未満".format(warn_percent),
    }


def find_container_usage(pod_metrics_items, namespace=ARGOCD_NAMESPACE,
                         pod=CONTROLLER_POD, container=CONTROLLER_CONTAINER):
    """collect_pod_metrics() の返す items から対象コンテナの usage 文字列を探す。

    見つからなければ None (metrics-server の普通の欠落)。quantity のパース失敗は
    黙って捨てない — 呼び出し側に例外を見せて reason に残させる。
    """
    if not isinstance(pod_metrics_items, list):
        raise TypeError("pod_metrics items はリスト: {!r}".format(pod_metrics_items))
    for item in pod_metrics_items:
        if not isinstance(item, dict):
            continue
        if item.get("namespace") != namespace or item.get("name") != pod:
            continue
        for c in item.get("containers") or []:
            if isinstance(c, dict) and c.get("name") == container:
                return c.get("memory")
    return None


def find_container_limit(pod_object, container=CONTROLLER_CONTAINER):
    """実機 pod GET の応答から対象コンテナの memory limit quantity を探す。

    見つからなければ None。spec 側の値が values.yaml 由来であることが
    この経路で保証される (ハードコードしない)。
    """
    if not isinstance(pod_object, dict):
        return None
    for c in (pod_object.get("spec") or {}).get("containers") or []:
        if isinstance(c, dict) and c.get("name") == container:
            return (c.get("resources") or {}).get("limits", {}).get("memory")
    return None


def build_report(pod_metrics_items, pod_object, warn_percent):
    """latest.json の `argocd` セクションの中身を組む。純関数。

    pod_metrics_items: collect_pod_metrics() の返したリスト (取得失敗時は report.py 側が
    error を raise するため、ここには list 以外は来ない想定。来ても TypeError で落とす)
    pod_object: k8s_get() の生応答 (spec.containers[].resources.limits.memory を読む)
    warn_percent: coerce_warn_percent() を通した閾値
    """
    checked = coerce_warn_percent(warn_percent)
    section = {
        "namespace": ARGOCD_NAMESPACE,
        "pod": CONTROLLER_POD,
        "container": CONTROLLER_CONTAINER,
        "warn_percent": checked,
        "limit_source": "spec.containers[].resources.limits.memory (live pod)",
        "status": None,
        "limit_usage_percent": None,
        "usage_bytes": None,
        "limit_bytes": None,
        "reason": None,
    }

    raw_usage = find_container_usage(pod_metrics_items)
    try:
        usage = parse_quantity_bytes(raw_usage)
    except (TypeError, ValueError) as e:
        section["status"] = "no_data"
        section["reason"] = "使用量サンプルを読めない: {}".format(e)
        return section
    section["usage_bytes"] = usage

    raw_limit = find_container_limit(pod_object)
    if raw_limit is None:
        result = judge(usage, None, checked)
    else:
        try:
            limit = parse_quantity_bytes(raw_limit)
        except (TypeError, ValueError) as e:
            section["status"] = "unconfigured"
            section["reason"] = "実機 pod の limit を読めない: {}".format(e)
            return section
        section["limit_bytes"] = limit
        result = judge(usage, limit, checked)

    section["status"] = result["status"]
    section["limit_usage_percent"] = result["limit_usage_percent"]
    section["reason"] = result["reason"]
    return section

"""Project CR を全件ダンプし、restic が拾う 1 ファイルへ決定的に書き出す。

なぜ要るか (設計 docs/design/state-out-of-git Phase 0b): k3s のデータストアは
kine/sqlite (`/var/lib/rancher/k3s/server/db/state.db`) で etcd ではないため
`k3s etcd-snapshot` が使えず、既存の restic CronJob 6 本はどれもアプリの PVC しか
見ていない。Phase 4b で projects.json を止めた瞬間、プロジェクトの記録は
**クラスタにしか存在しなくなる**。node01 の消失が記録の全損になる経路をここで塞ぐ。

state.db を丸ごと掬う案は採らない。hostPath で state.db を読む Pod は事実上
全 Secret を読めるのと同じで、この 1 個のために作る攻撃面として大きすぎる。
守るべきはプロジェクトの記録であって、クラスタ全体ではない。

**書き出しの形は `kubectl apply -f` で戻せる v1 List** (projects.json の形ではない)。
理由: 4b 以降の正は CR であって projects.json ではなく、復元とは「CR を作り直す」
こと。CR の spec は projects.json の 1 エントリをキー名そのまま載せてあるので
(apps/autopilot/crd-project.yaml)、projects.json 相当が要るときは
`jq '[.items[].spec]'` で落とせる — CR 形式の方が情報が広い。

決定的であること: 出力は CR の中身だけの関数で、時刻もホスト名も混ぜない。
同じ CR 集合なら同じバイト列になるので、restic は変わっていない回に何も足さない。
そのために (1) items を metadata.name でソート、(2) json.dumps(sort_keys=True)、
(3) 復元の邪魔になるうえ毎ビート変わる metadata (resourceVersion / uid /
creationTimestamp / generation / managedFields …) を落とす、の 3 つを守る。

fail-closed: CRD の同期失敗や RBAC 事故で空になったものを「正しい最新」として
上書きすると、記録が静かに消える。0 件・前回比の急減・floor 割れはすべて
書き出さずに落とす (check_export)。

cluster 外 (テスト) からも import できるよう、ServiceAccount トークンはモジュール
top ではなく呼ばれたときに読む (ops/tests/test_export_projects.py が import する)。
"""

import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

GROUP = "autopilot.homelab.hikuohiku.dev"
VERSION = "v1"
PLURAL = "projects"

NAMESPACE = os.environ.get("PROJECT_NAMESPACE", "autopilot")
OUT_PATH = os.environ.get("OUT_PATH", "/export/projects.json")
# 前回のスナップショットを restic が書き戻したもの (初回・取得失敗時は存在しない)
PREVIOUS_PATH = os.environ.get("PREVIOUS_PATH", "/export/previous.json")

# 前回比の許容下限。終端 CR は消さない設計 (live set は lifecycle ラベルで切る) なので
# 件数は本来単調に増える。10% の余地は「id を間違えて作った CR を数個消す」運用
# (apps/autopilot/rbac.yaml の project-writer が delete を持つ理由) を通すため。
MIN_RATIO_OF_PREVIOUS = 0.9
# 前回が分からないとき (初回・restic 側の取得失敗) だけ効く最後の網。
# 2026-08-24 実測 112 件に対する概数。**割ってはいけない線**であって目標ではないので、
# 件数が増えても上げ直さなくてよい (緩い方向にしか腐らず、正しい実行を止めない)。
MIN_PROJECT_COUNT = int(os.environ.get("MIN_PROJECT_COUNT", "100"))

PAGE_LIMIT = 500

# 残す metadata。ここに挙げないものは全部落とす (allowlist)。
# name/namespace は同一性そのもの。labels には live set の selector (lifecycle) が載る。
# annotations は heart が今後付けうるので残すが、apply の残骸だけは捨てる。
KEEP_METADATA = ("name", "namespace", "labels", "annotations")
DROP_ANNOTATIONS = ("kubectl.kubernetes.io/last-applied-configuration",)

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def _k8s_get(path):
    """SA トークンで k8s API を GET する。import 時ではなく呼ばれたときに鍵を読む。"""
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with open(os.path.join(SA_DIR, "token")) as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    req = urllib.request.Request(
        "https://{}:{}{}".format(host, port, path),
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.load(resp)


def list_projects(get=_k8s_get, namespace=None):
    """Project CR を全件返す。continue トークンを最後まで追う。

    ページを追い切らずに打ち切ると「全件のつもりで一部だけ」を正として上書きしうる。
    HTTP エラーは握り潰さない (例外がそのまま Job の失敗になる)。
    """
    namespace = namespace or NAMESPACE
    base = "/apis/{}/{}/namespaces/{}/{}".format(GROUP, VERSION, namespace, PLURAL)
    items = []
    cont = None
    while True:
        query = {"limit": PAGE_LIMIT}
        if cont:
            query["continue"] = cont
        data = get(base + "?" + urllib.parse.urlencode(query))
        items.extend(data.get("items") or [])
        cont = (data.get("metadata") or {}).get("continue")
        if not cont:
            return items


def sanitize(item):
    """1 件の CR を復元可能かつ決定的な形へ削る。

    落とすもの: resourceVersion / uid / creationTimestamp / generation /
    managedFields / ownerReferences / selfLink / finalizers と status。
    どれもクラスタが付けるもので、残すと (a) apply が resourceVersion 衝突で落ち、
    (b) 毎ビート値が変わるので中身が同じでも差分が出る。
    """
    meta = item.get("metadata") or {}
    kept = {}
    for key in KEEP_METADATA:
        value = meta.get(key)
        if value in (None, {}, ""):
            continue
        if key == "annotations":
            value = {k: v for k, v in value.items() if k not in DROP_ANNOTATIONS}
            if not value:
                continue
        kept[key] = value
    return {
        "apiVersion": "{}/{}".format(GROUP, VERSION),
        "kind": "Project",
        "metadata": kept,
        "spec": item.get("spec") or {},
    }


def build_document(items):
    """`kubectl apply -f` がそのまま食える v1 List を組む。並びは name 順で固定。"""
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": sorted(
            (sanitize(i) for i in items),
            key=lambda o: o["metadata"].get("name", ""),
        ),
    }


def serialize(doc):
    """バイト列まで決定的にする。sort_keys が無いと dict の並び次第で差分が出る。"""
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def previous_count(path=None):
    """前回のスナップショットの件数。無い・読めない・壊れているときは None (不明)。

    不明を 0 とみなすと「初回は必ず落ちる」か「急減を見逃す」のどちらかになるので、
    不明のまま返して呼び先で floor 側へ倒す。
    """
    path = path or PREVIOUS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    return len(items) if isinstance(items, list) else None


def check_export(count, previous, minimum=None):
    """書き出してよいかを判定し、駄目な理由を文字列のリストで返す (空なら OK)。"""
    minimum = MIN_PROJECT_COUNT if minimum is None else minimum
    problems = []
    if count == 0:
        problems.append(
            "Project CR が 0 件。CRD の同期失敗か RBAC 事故の可能性が高い。"
            "空を『正しい最新』として上書きすると記録が静かに消えるので書き出さない"
        )
        return problems
    if previous is None:
        if count < minimum:
            problems.append(
                "Project CR が {} 件で floor {} 件を下回る "
                "(前回の件数が不明なので floor で判定した)".format(count, minimum)
            )
    elif count < previous * MIN_RATIO_OF_PREVIOUS:
        problems.append(
            "Project CR が {} 件。前回 {} 件から {:.0%} 以上減っている。"
            "終端 CR を消さない設計では件数は減らないはずなので、取得の失敗を疑う".format(
                count, previous, 1 - MIN_RATIO_OF_PREVIOUS
            )
        )
    return problems


def main():
    items = list_projects()
    problems = check_export(len(items), previous_count())
    if problems:
        for p in problems:
            print("FATAL: " + p, file=sys.stderr)
        return 1
    payload = serialize(build_document(items))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(payload)
    print("exported {} projects -> {} ({} bytes)".format(
        len(items), OUT_PATH, len(payload.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

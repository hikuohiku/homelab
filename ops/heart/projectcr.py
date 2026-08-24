"""projects.json のエントリを Project CR の姿に変換する (設計 state-out-of-git Phase 4a)。

ここは純関数だけ。k8s を叩くのは呼び出し側 (heart.beat) で、CRD は
apps/autopilot/crd-project.yaml。

今の段階では **正は projects.json のまま**で、CR は写し。読み手の切り替えは 4b。
"""

GROUP = "autopilot.homelab.hikuohiku.dev"
VERSION = "v1"
API_VERSION = f"{GROUP}/{VERSION}"
KIND = "Project"
PLURAL = "projects"

# 作業集合を問い合わせ側で切るためのラベル (設計「live set を selector で切る」)。
# 終端プロジェクトは消さないので、`kubectl get projects -l lifecycle=live` が
# 非終端だけを返す形にしておく
LIVE = "live"
TERMINAL = "terminal"


def cr_name(project_id):
    """CR 名はプロジェクト id の小文字。id (P-0102) は RFC 1123 の名前になる"""
    return str(project_id).lower()


def labels(project, terminal_states):
    return {
        "state": project.get("state", ""),
        "lifecycle": TERMINAL if project.get("state") in terminal_states else LIVE,
    }


def to_cr(project, namespace, terminal_states):
    """1 エントリを CR にする。spec はキー名を変えずそのまま載せる。"""
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": cr_name(project["id"]),
            "namespace": namespace,
            "labels": labels(project, terminal_states),
        },
        "spec": dict(project),
    }


def plan(doc, existing, namespace, terminal_states):
    """(書くべき CR, doc から消えた CR 名) を返す。

    書くのは中身が変わったものだけ。112 件を毎ビート送り直すと、変わらない
    リソースにも API 呼び出しが要る (server-side apply 自体は no-op でも、
    ビートあたり 112 リクエストは無駄)。

    消えたプロジェクト (CR にあるが doc に無い) は **消さない**。git 側が正で
    ある間、CR の削除だけが片道の操作で、doc の一時的な欠落 (checkout の失敗や
    部分的な書き込み) がそのまま記録の消失になる。終端も含めて CR は残すのが
    設計の前提でもある (「終端 CR は消さない — live set は selector で切る」)。
    ここでは名前を返すだけで、呼び出し側はログに出す。削除条件は CR が正になる
    4b で決める。
    """
    desired = [to_cr(p, namespace, terminal_states) for p in doc.get("projects", [])]
    by_name = {}
    for item in existing:
        name = (item.get("metadata") or {}).get("name")
        if name:
            by_name[name] = item
    write = []
    for cr in desired:
        cur = by_name.get(cr["metadata"]["name"])
        if cur is None or not _same(cur, cr):
            write.append(cr)
    orphans = sorted(set(by_name) - {cr["metadata"]["name"] for cr in desired})
    return write, orphans


def _same(current, desired):
    """CR が既に望みの姿かどうか。サーバが足すフィールドは見ない。"""
    cur_labels = (current.get("metadata") or {}).get("labels") or {}
    want_labels = desired["metadata"]["labels"]
    if any(cur_labels.get(k) != v for k, v in want_labels.items()):
        return False
    return current.get("spec") == desired["spec"]

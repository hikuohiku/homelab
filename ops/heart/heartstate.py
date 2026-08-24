"""projects doc のうち CR に載らない部分を持つ CR (設計 4b-2)。

Project CR は 1 プロジェクト 1 個なので、doc のトップレベルにあるスカラ
(`stop_engaged` / `last_curriculum_at` / `last_activity_at` …) の置き場が無い。
そのままにすると projects.json を畳めない。

**ConfigMap にしなかった理由は RBAC**。`autopilot-writer` (宣言制注入でプロジェクト
Job に渡る SA) は configmaps に `*` を持っていて、名前で穴を塞ぐ手段が RBAC には
無い。ConfigMap に置くと「Job が stop_engaged を書き換えられる」= 単一書き手が
また慣習に戻る。API グループ `autopilot.homelab.hikuohiku.dev` は
autopilot-writer に一切渡していないので、CR にすれば API が止める。

置き場を PVC にしなかったのは外に読み手が居るため:

  - `stop_engaged` — ダッシュボードが「止まっているか」を出す
  - `stop_engaged` / `last_curriculum_at` — コアの shadow が起動条件に使う

CRD は apps/autopilot/crd-heartstate.yaml。コントローラは無い。
ここは純関数だけで、API を叩くのは呼び出し側 (heart.beat)。
"""

from .projectcr import API_VERSION

KIND = "HeartState"
PLURAL = "heartstates"
# 1 namespace に 1 個だけ。名前を固定するのが「唯一の状態」の表現になる
NAME = "heart"

# CR に載せないキー。projects は Project CR 側が正で、二重に持つと
# どちらが本当か分からなくなる
EXCLUDED = ("projects",)


def scalars(doc):
    """doc から CR に載せる部分だけを抜く (純関数)。"""
    return {k: v for k, v in doc.items() if k not in EXCLUDED}


def to_cr(namespace, doc):
    """server-side apply に渡す HeartState を組む (純関数)。"""
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"name": NAME, "namespace": namespace},
        "spec": scalars(doc),
    }


def from_cr(cr):
    """HeartState から doc スカラを取り出す (純関数)。無ければ {}。

    **例外にしない**のは、CR がまだ無い最初のビートが正常だから (中身は
    「stop_engaged は false、まだ何もしていない」と読める既定へ落ちる)。
    プロジェクトが消えたかどうかの fail-closed は Project CR 側で見る。
    """
    spec = (cr or {}).get("spec")
    return dict(spec) if isinstance(spec, dict) else {}

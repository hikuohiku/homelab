"""projects.json のエントリを Project CR の姿に変換する (設計 state-out-of-git Phase 4a)。

ここは純関数だけ。k8s を叩くのは呼び出し側 (heart.beat) で、CRD は
apps/autopilot/crd-project.yaml。

4b-2a で **読み手は全員 CR を読む**ようになった (下段の「読み出し」)。書き込みは
projects.json / archive.jsonl にも残っているので、git 側は正しい写しのまま。
書き込みを止めるのが 4b-2b。
"""

import copy

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
    # 棄却案の CR は projects.json に居ないのが正常なので、orphan に数えない
    # (数えると毎ビート 250 件の名前をログに吐く)
    wanted = {cr["metadata"]["name"] for cr in desired}
    orphans = sorted(
        name for name, item in by_name.items()
        if name not in wanted
        and (item.get("spec") or {}).get("state") != "rejected"
    )
    return write, orphans


def _same(current, desired):
    """CR が既に望みの姿かどうか。サーバが足すフィールドは見ない。"""
    cur_labels = (current.get("metadata") or {}).get("labels") or {}
    want_labels = desired["metadata"]["labels"]
    if any(cur_labels.get(k) != v for k, v in want_labels.items()):
        return False
    return current.get("spec") == desired["spec"]


# --- 棄却案 (設計 state-out-of-git「棄却された案も CR にする」) ---

# 1 ビートに取り込む棄却案の上限。台帳には 250 件超あり、初回は全件が「新規」に
# なる。まとめて送ると 1 ビートの中に 250 回の PATCH が入り、その間 heart は他に
# 何もしない (ビートは 120s)。分割すれば 1 時間ほどで収束し、収束後は
# plan_rejected が 0 件を返すので恒常的な費用にはならない
REJECTED_BATCH_LIMIT = 25

# 棄却は定義上つねに終端。labels() へ渡す終端集合をここで閉じておくと、
# projectcr が statefiles を import せずに済む (この島は純関数だけで保つ)
REJECTED_TERMINAL = ("rejected",)

# 棄却案が持たないフィールドの既定値。REQUIRED_PROJECT_FIELDS を state ごとに
# 変えない代わりにここで埋める (理由は statefiles.py の同定数のコメント)。
# branch が空文字なのは「枝が一度も切られなかった」の素直な表現で、
# validate_projects の「非終端は project/ で始まること」は終端なので当たらない
REJECTED_BRANCH = ""


def latest_records(records):
    """id ごとに最後の行だけを残す (facts.load_adopted_specs と同じ規則)。

    台帳は追記のみで、同じ id が複数行あるときは後の行が現在の判断
    (実際 P-0028 / P-0029 は棄却の行の後に採択の行が積まれている)。
    """
    out = {}
    for rec in records:
        if isinstance(rec, dict) and rec.get("id"):
            out[rec["id"]] = rec
    return out


def to_rejected_project(record):
    """台帳の棄却行を projects.json の 1 エントリと同じ形にする (純関数)。

    形を揃えるのは to_cr / CRD / バックアップを 1 本のままにしておくため。
    立案時の spec は丸ごと `spec` に載る — reject_reason / improve_hint は
    そこに居て、これが判定の教師信号が生成へ戻る唯一の経路
    (ops/runner/runner.py build_archive_records)。
    """
    proposed_at = str(record.get("proposed_at") or "")
    return {
        "id": record["id"],
        "title": record.get("title", ""),
        "state": "rejected",
        "branch": REJECTED_BRANCH,
        "irreversible": bool(record.get("irreversible")),
        "capabilities": list(record.get("capabilities") or []),
        "touches_apps": bool(record.get("touches_apps")),
        "verify": list(record.get("verify") or []),
        "confidence": record.get("confidence", "unsure"),
        # 予算は消費していない。採択案と同じキーで 0 を置く
        "budget": {"used_tokens": 0},
        # created は日付だけ。proposed_at が無い古い行では空文字になる
        "created": proposed_at[:10],
        "spec": dict(record),
    }


def plan_rejected(records, existing, namespace, live_ids, limit=REJECTED_BATCH_LIMIT):
    """台帳の棄却行のうち、まだ CR になっていないものを最大 limit 件返す (純関数)。

    live_ids (= projects.json に居る id) は **必ず飛ばす**。採択されたものは
    projects.json 側が正で、そちらの CR を棄却で上書きすると走行中の
    プロジェクトの状態が消える。

    既にある CR は中身を比べない — 棄却案は二度と変わらないので、毎ビート
    250 件分の spec を突き合わせる意味が無い (名前の有無だけを見る)。

    **新しい id から入れる**。収束には 1 時間ほどかかるので、その間に立案役が
    読めるのが「直近に何がなぜ落ちたか」になるようにする (古い案から埋めると
    一番効く信号が最後に届く)。
    """
    have = {(item.get("metadata") or {}).get("name") for item in existing}
    out = []
    for pid, rec in sorted(latest_records(records).items(), reverse=True):
        if rec.get("adopted") or pid in live_ids:
            continue
        if cr_name(pid) in have:
            continue
        out.append(to_cr(to_rejected_project(rec), namespace, REJECTED_TERMINAL))
        if len(out) >= limit:
            break
    return out


# --- 読み出し (設計 state-out-of-git 4b-2a「読み手を CR へ」) ---

# 読み手が使う selector。**問い合わせ側で切る**のが設計の前提で、
# 終端 250 件超を毎回引いて手元で捨てる読み手を作らない。
LIVE_SELECTOR = f"lifecycle={LIVE}"
# 棄却案だけを外す。ダッシュボードや reconcile が見たいのは「一度は動いた案」で、
# それには終端の delivered / stalled / vetoed も含まれる
NOT_REJECTED_SELECTOR = "state!=rejected"
# 逆向き。棄却案の取り込みが済んでいるかを見るときだけ使う (heart.plan_rejected_crs)
REJECTED_SELECTOR = "state=rejected"


def projects_from_items(items):
    """CR の一覧を projects.json の projects 配列と同じ形に戻す (純関数)。

    CR の `spec` が projects.json の 1 エントリそのもの (to_cr と対称)。
    その中の `spec` 子は立案時の spec で **別物**なので取り違えないこと。
    id を持たない CR は落とす — 突き合わせの鍵が無いものは読み手の役に立たない。
    """
    out = [
        item["spec"]
        for item in items
        if isinstance(item.get("spec"), dict) and item["spec"].get("id")
    ]
    out.sort(key=lambda p: p["id"])
    return out


def adopted_specs_from_items(items):
    """採択済み案の **立案時 spec** を {id: spec} で返す (純関数)。

    台帳 (archive.jsonl) の `adopted` 行と同じものが CR の `spec.spec` に載る
    (reconcile._register_spec が dict(spec) を丸ごと置いている)。
    棄却案は除く — 呼び出し側は NOT_REJECTED_SELECTOR で引く前提だが、
    selector を通さない経路から渡っても混ざらないようにここでも落とす。
    """
    out = {}
    for project in projects_from_items(items):
        if project.get("state") == "rejected":
            continue
        spec = project.get("spec")
        if isinstance(spec, dict) and spec.get("id"):
            out[spec["id"]] = spec
    return out


def proposal_digest(items):
    """立案役が読む「過去に何が出て、なぜ落ちたか」を新しい順で返す (純関数)。

    **棄却案を含む唯一の読み出し**。reject_reason / improve_hint は判定の教師信号が
    生成に戻る唯一の経路 (ops/runner/runner.py build_archive_records) なので、
    ここを痩せさせると同型再提案が常態化する。

    why / dod / verify は載せない — 立案が要るのは「既出か」と「死因」だけで、
    全文を載せると 400 件で数 MB になる (コアの homelab_proposals と同じ判断)。
    """
    rows = []
    for project in projects_from_items(items):
        spec = project.get("spec") if isinstance(project.get("spec"), dict) else {}
        rows.append({
            "id": project["id"],
            "title": project.get("title") or spec.get("title", ""),
            "cell": spec.get("cell") or [],
            # adopted は棄却案の spec にしか無い。state から決めるのが確実
            "adopted": project.get("state") != "rejected",
            "state": project.get("state", ""),
            "proposed_at": spec.get("proposed_at", ""),
            "proposed_by": spec.get("proposed_by", ""),
            "reject_reason": spec.get("reject_reason", ""),
            "improve_hint": spec.get("improve_hint", ""),
        })
    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows


# --- doc の復元 (設計 state-out-of-git 4b-2b「projects.json を止める」) ---

# doc のトップレベルのうちプロジェクト一覧でないもの。CR には載らないので
# HeartState CR (ops/heart/heartstate.py) が持つ
DOC_DEFAULTS = {"version": 1, "projects": [], "chores": []}


def working_set(items):
    """状態機械が触る作業集合 (純関数)。棄却案は **含めない**。

    250 件超の墓標を混ぜると decide が毎ビート全部を舐めるうえ、
    validate_projects の不変条件も墓標のために緩める羽目になる。棄却理由を
    読むのは立案役で、そちらは proposal_digest が別に返す。

    **写しを返す**。CR の spec をそのまま返すと doc の更新が existing 側にも
    及び、plan() が「変わっていない」と判断して書き込みを丸ごと飛ばす。
    """
    return [
        copy.deepcopy(p)
        for p in projects_from_items(items)
        if p.get("state") != "rejected"
    ]


def doc_from_crs(items, scalars=None):
    """Project CR の一覧と HeartState のスカラから projects doc を組み立てる (純関数)。

    projects.json を読み込んでいた heart.beat の入り口の置き換え。
    scalars 側の projects は無視する — 一覧の正は CR ひとつだけにする。
    """
    doc = dict(DOC_DEFAULTS)
    for key, value in (scalars or {}).items():
        if key != "projects":
            doc[key] = value
    doc["projects"] = working_set(items)
    doc.setdefault("chores", [])
    doc.setdefault("version", 1)
    return doc

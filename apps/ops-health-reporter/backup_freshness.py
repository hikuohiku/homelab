"""restic backup の鮮度計 (P-0157)。

#49 型の事故で本当に怖いのは「backup 子 Job の失敗がアプリを赤くする」ことではなく、
**オーケストレータ CronJob 自身が死んで「新しい Job が生まれない」静停止**である。
静停止では失敗も Degraded も起きず、全アプリは緑のままバックアップだけが止まる。
このモジュールは 5 つの restic backup 経路について「最後に成功してから何時間経ったか」
を計算し、rules.json の閾値と比較した判定だけを担う純関数群で、クラスタやネットワークに
触れない (download_budget.py と同じ分離)。

- 集約側 (apps/ops-health-reporter/report.py): batch API から CronJob / Job を読んで
  build_report() に渡し、返り値を latest.json / history jsonl の `backup_freshness`
  キーへ載せる

## 閾値の単一情報源とフォールバック

warn 閾値は repo の ops/rules.json (backup_freshness.warn_hours) が正で、report.py は
GitHub Contents API 経由で base ブランチ (main) からそれを読む。ConfigMap への embed
(kustomize configMapGenerator の root 外ファイル参照) は kubectl/kustomize 標準の
load restrictor が拒むため採らない — ArgoCD 既定の LoadRestrictionsNone に賭ける
変更は reporter 全体の sync を壊しかねない。ops-health-report ブランチ上の rules.json
は分岐時点で凍結されるため使わない。読み込みに失敗したときのフォールバックが
DEFAULT_WARN_HOURS で、値は heartbeat.stale_seconds と同じ「コメントで結合を綴る」
方式で rules.json と揃えて管理する — rules.json の warn_hours を変えたらここも
同時に変えること

## 成功時刻のソース (DoD(1)。選定理由)

主に **(b) CronJob の status.lastSuccessfulTime** を読む。子 Job が GC された後も残る
実績がある (T-0117、journal run #183/#205 で 2026-08-06 実測)。特に coder workspace home
の子 Job は ttlSecondsAfterFinished=3600 で 1 時間で消えるため、「今朝成功したか」の観測点
としては子 Job より頑健。lastSuccessfulTime が読めない場合のみ、フォールバックで
**(a) Complete=True の子 Job の completionTime の max** を使う (ownerReferences の
kind=CronJob + name で所有関係を判定する。label は k8s バージョンで接頭辞が変わった
経緯があり ownerReferences の方が安定)。

## 動的 PVC 対象の扱い (DoD(3))

coder workspace home の実体 PVC (`coder-<workspace-id>-home`) はコントローラが動的に
作るため manifest がリポジトリに存在しない (test_backup_coverage.py の既知の死角)。
個々の PVC を見ず、**オーケストレータ CronJob `coder-workspace-home-backup` 自身の
最終成功で代用する** (T-0078/T-0117 が同じ代用をやった前例)。測定単位が元々 CronJob
なので他 4 経路と同じ形で自然に成立する。

report.py と同じく標準ライブラリのみで動く。import 副作用を持たない (report.py と違い
ServiceAccount token を読まないので、cluster 外の unit test から importlib で直接
ロードできる)。
"""

import datetime
import json

# 閾値の単一情報源は repo の ops/rules.json (backup_freshness.warn_hours) で、
# report.py が GitHub Contents API 経由で base ブランチから読む (冒頭の節参照)。
# この既定値は rules.json 読み込みに失敗したときのフォールバックであり、値は
# heartbeat.stale_seconds と同じ「コメントで結合を綴る」方式で揃えてある —
# rules.json の warn_hours を変えたらここも同時に変えること
DEFAULT_WARN_HOURS = 72

# 通常周期は全 5 経路が日次 CronJob (24h)。3 倍の 72h で warn。
# schedule に spec.timeZone が無い CronJob は node01 の JST で評価される
# (substrate.md T-0125) が、鮮度は実際の時刻差だけを見るので評価 TZ には依存しない

# 監視対象の 5 経路。manifest 実測は PROJECT.md (initializer, 2026-08-23)。
# 順序は latest.json の出力順でもある
REPOSITORIES = [
    {"repo": "vaultwarden", "namespace": "vaultwarden",
     "cronjob": "vaultwarden-restic-backup"},
    {"repo": "coder-postgres", "namespace": "coder", "cronjob": "coder-restic-backup"},
    {"repo": "immich", "namespace": "immich", "cronjob": "immich-restic-backup"},
    {"repo": "coder-workspace-home", "namespace": "coder",
     "cronjob": "coder-workspace-home-backup"},
    {"repo": "syncthing", "namespace": "syncthing", "cronjob": "syncthing-restic-backup"},
]


def parse_iso(value):
    """RFC3339 (k8s の timestamp フィールド) を UTC の datetime へ。失敗時 None。

    python 3.11 未満の fromisoformat は "Z" 接尾を受け付けないため自前で置換する
    (CI とコンテナの python バージョン差に左右されないように)。
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # tz 無しは UTC と見なす (k8s は常に RFC3339 + Z を返すが防御的に)
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def hours_since(last_success_at, now):
    """成功時刻 (ISO 文字列または datetime) → 経過時間 (時間, float)。

    未成功 (None / 読めない時刻) は None。now が成功時刻より過去に見える場合は
    clock skew として 0.0 に丸める — 鮮度計が skew だけで鳴ってはいけない
    (heartbeat judge() の skew 扱いと同じ倒し方)。
    """
    if isinstance(last_success_at, str):
        last_success_at = parse_iso(last_success_at)
    if not isinstance(last_success_at, datetime.datetime):
        return None
    if not isinstance(now, datetime.datetime):
        return None
    delta = now - last_success_at
    return max(0.0, delta.total_seconds() / 3600.0)


def judge(hours, warn_hours=DEFAULT_WARN_HOURS):
    """経過時間 (時間) を閾値に対して判定する。

    status:
      ok            閾値未満
      warn          閾値以上 (境界含む、鳴る側に倒す — download_budget.judge() 同型)
      no_data       測定できない (未成功・時刻 unreadable)
      unconfigured  閾値が正の数でない (rules.json の値が壊れている)
    """
    if hours is None:
        return "no_data"
    valid = isinstance(warn_hours, (int, float)) and not isinstance(
        warn_hours, bool) and warn_hours > 0
    if not valid:
        return "unconfigured"
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return "no_data"
    return "warn" if hours >= warn_hours else "ok"


def extract_last_success(cronjob_item, owned_job_items):
    """CronJob 1 件 (+ 所有する子 Job 群) から最後の成功時刻を (ISO 文字列, source) で返す。

    主系統 (b) status.lastSuccessfulTime、副系統 (a) Complete=True の子 Job の
    completionTime の max (モジュール冒頭の選定理由参照)。どちらも読めなければ
    (None, None)。壊れた時刻文字列は parse_iso() で静かに捨てる。
    所有判定はここでも再確認する (ownerReferences の kind=CronJob + name。
    label は k8s バージョンで接頭辞が変わった経緯があり ownerReferences の方が安定)。
    """
    if not isinstance(cronjob_item, dict):
        return None, None
    meta = cronjob_item.get("metadata") or {}
    candidates = []
    lst = (cronjob_item.get("status") or {}).get("lastSuccessfulTime")
    if isinstance(lst, str):
        candidates.append((lst, "cronjob.status.lastSuccessfulTime"))
    job_times = []
    for job in owned_job_items or []:
        if not isinstance(job, dict):
            continue
        jmeta = job.get("metadata") or {}
        owners = [
            r for r in (jmeta.get("ownerReferences") or [])
            if r.get("kind") == "CronJob" and r.get("name") == meta.get("name")
        ]
        if not owners:
            continue
        status = job.get("status") or {}
        complete = any(
            c.get("type") == "Complete" and c.get("status") == "True"
            for c in (status.get("conditions") or [])
        )
        completion_time = status.get("completionTime")
        # completionTime は Failed な Job にも付くので Complete 条件で絞る
        if complete and isinstance(completion_time, str) \
                and parse_iso(completion_time) is not None:
            job_times.append(completion_time)
    if job_times:
        candidates.append((max(job_times), "job.status.completionTime"))
    for value, source in candidates:
        if parse_iso(value) is not None:
            return value, source
    return None, None


def coerce_warn_hours(value, default=DEFAULT_WARN_HOURS):
    """rules.json から読んだ warn_hours の検査。正の数以外は default に倒す。

    bool は int の派生なので明示的に弾く (True=1 で閾値化したら即鳴きになる)。
    壊れた値で決め打ちせず default を返す (download_budget.DEFAULT_DAILY_CAP_BYTES
    の「設定が無いなら正直に既定で倒す」と同じ思想)。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return value
    return default


def load_warn_hours(path, default=DEFAULT_WARN_HOURS):
    """rules.json ファイルから backup_freshness.warn_hours を読む。読めなければ default。

    report.py は cluster 外から import できない (ServiceAccount token を import 時に
    読む) ので、ファイル読み込みはここに置いて unit test 可能にしてある。
    """
    try:
        with open(path) as f:
            value = json.load(f).get("backup_freshness", {}).get("warn_hours")
        return coerce_warn_hours(value, default)
    except (OSError, ValueError):
        return default


def build_entry(spec, cronjob_item, owned_job_items, now, warn_hours):
    """監視対象 1 経路 → latest.json の backup_freshness 要素 1 件。

    収集できなかった経路は黙って落とさず error エントリとして載せる
    (collect_download_budget() と同じ思想)。verify (#2) の契約上、全エントリは
    error のときも repo / hours_since_success キーを持つ (値は None)。
    """
    entry = {
        "repo": spec["repo"],
        "namespace": spec["namespace"],
        "cronjob": spec["cronjob"],
        "last_success_at": None,
        "hours_since_success": None,
        "status": "error",
    }
    if cronjob_item is None:
        entry["detail"] = (
            "CronJob {} が cluster 内から読めない (未 sync・削除・RBAC 不足)".format(
                spec["cronjob"])
        )
        return entry
    iso, source = extract_last_success(cronjob_item, owned_job_items)
    if iso is None:
        entry["status"] = "no_data"
        entry["detail"] = (
            "最後の成功時刻が読めない (一度も成功していないか記録が GC 済み)"
        )
        return entry
    entry["last_success_at"] = iso
    entry["source"] = source
    hours = hours_since(iso, now)
    entry["hours_since_success"] = round(hours, 2) if hours is not None else None
    entry["status"] = judge(hours, warn_hours)
    return entry


def build_report(cronjob_items, job_items, now=None, warn_hours=DEFAULT_WARN_HOURS):
    """batch API の CronJob/Job 一覧 → latest.json の `backup_freshness` キーの中身。

    cronjob_items / job_items は k8s_get("/apis/batch/v1/cronjobs") 等の items
    (クラスタ全体のリスト)。監視対象 5 経路を REPOSITORIES の順に出す。子 Job の
    所有判定は ownerReferences (kind=CronJob) で行う。
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    cronjobs = {}
    for item in cronjob_items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        cronjobs[(meta.get("namespace"), meta.get("name"))] = item
    owned_jobs = {}
    for item in job_items or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace")
        for ref in meta.get("ownerReferences") or []:
            if ref.get("kind") == "CronJob":
                owned_jobs.setdefault((ns, ref.get("name")), []).append(item)
    return [
        build_entry(
            spec,
            cronjobs.get((spec["namespace"], spec["cronjob"])),
            owned_jobs.get((spec["namespace"], spec["cronjob"]), []),
            now,
            warn_hours,
        )
        for spec in REPOSITORIES
    ]

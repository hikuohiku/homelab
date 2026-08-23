#!/usr/bin/env python3
"""OpenClaw 朝 brief (P-0174) — homelab の昨日の変化を Telegram 1 通に畳む。

秘書の「おはようございます」。Discord (ops/heart/notify.py) が予告と納品のための
業務チャネルなのに対し、この brief は生活者への接点を作る最初の習慣。既存のデータ源
(ops-health-report ブランチの latest.json と history jsonl、main の merge 履歴) だけを
読み、新規データは作らない。私的データは読まない (lethal trifecta に触れない)。

送信文は最大 3 行:
  (a) 納品 — 前日 (JST) に main へ merge した PR 数とうち project/* ブランチ数
  (b) 健全性 — ArgoCD アプリ health の前日最終スナップショットからの変化
  (c) backup — backup_listing の最新 mtime の鮮度
データが欠けた節はその行ごと省く (「不明」の埋め文字で行数を守るより、
正直に減らす。PROJECT.md 方針 2)。全部欠けた場合は送信せず Job を失敗させ、
k8s 側で可視化する (空の挨拶で沈黙を偽装しない)。

構成:
- 純関数群は import 副作用ゼロ。unit test からは importlib で直接ロードする
  (ops/tests/test_download_budget.py・test_openclaw_bridge.py と同じ形)。
- main() (--name__ == "__main__" 時のみ) だけが env とネットワークに触れる。
  --dry-run なら Telegram へ送らず stdout に書き出すだけ。

Telegram 送信はこの brief 1 通のみで、受信はしない (受信は bridge.py P-0107 の領分)。
回数の予算は rules.json notify.telegram_morning_brief_per_day に明記してある
(Discord 即時送信の daily_budget とは別枠)。テスト: リポジトリルートから
`python3 -m unittest ops.tests.test_morning_brief`
"""

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

# node01 の time.timeZone に合わせて「朝」「前日」は JST で切る (PROJECT.md 前提)。
JST = datetime.timezone(datetime.timedelta(hours=9), name="JST")

# main の merge commit の 1 行目。GitHub web UI / gh pr merge のどちらも同型。
_MERGE_RE = re.compile(r"^Merge pull request #\d+ from \S*/project/")
_MERGE_ANY_PREFIX = "Merge pull request #"

# backup mtime がこの時間より古かったら行末に「古い」を添える。immich backup は
# 毎日 1 回走るため、正常時の朝 brief 時点でおよそ 21 時間前になる。36 時間 =
# 「1 回分を完全に取りこぼした」検知線 (2 日連続で鳴らさないための余裕込み)。
DEFAULT_STALE_HOURS = 36.0

# 健全性の行に名前を挙げる最大数。超えた分は「他 N 件」に畳む (3 行上限のため)。
MAX_CHANGE_NAMES = 3


def parse_iso(value):
    """ISO 8601 文字列 → UTC の aware datetime。壊れていれば None (例外を出さない)。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def simplify_commit(entry):
    """GitHub commits API の 1 要素 → {message, committed_at}。形が違えば None。

    純関数側はこの簡約形だけを扱うことで、API の細部 (author/committer の区別や
    sha 群) に依存しない。
    """
    if not isinstance(entry, dict):
        return None
    commit = entry.get("commit")
    if not isinstance(commit, dict):
        return None
    message = commit.get("message")
    committer = commit.get("committer")
    date = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(message, str) or not isinstance(date, str):
        return None
    return {"message": message, "committed_at": date}


def count_merges(commits, day):
    """simplify_commit() 済みリストを JST の day 1 日窓で集計する。

    returns {"total": merge commit 数, "project": うち project/* ブランチ由来数}。
    projects.json に納品時刻フィールドが無い (PROJECT.md 前提) ため、納品の証跡は
    main の merge commit 日時を使う。窓は [day 00:00, day+1 00:00) JST。
    壊れた要素・窓外は黙って捨てる (sum_window と同じ倒し方)。
    """
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=JST)
    end = start + datetime.timedelta(days=1)
    total = 0
    project = 0
    for entry in commits or []:
        if not isinstance(entry, dict):
            continue
        when = parse_iso(entry.get("committed_at"))
        message = entry.get("message")
        if when is None or not isinstance(message, str):
            continue
        if not (start <= when < end):
            continue
        if not message.startswith(_MERGE_ANY_PREFIX):
            continue
        total += 1
        if _MERGE_RE.match(message):
            project += 1
    return {"total": total, "project": project}


def app_health_map(apps):
    """latest.json applications[] → {name: health}。壊れた要素は捨てる。"""
    mapping = {}
    for app in apps or []:
        if not isinstance(app, dict):
            continue
        name = app.get("name")
        health = app.get("health")
        if isinstance(name, str) and name and isinstance(health, str) and health:
            mapping[name] = health
    return mapping


def health_changes(current_apps, prev_apps):
    """前日比の health 変化を [{name, from, to}] で返す (名前順で決定論的)。

    片側にしか無いアプリは from/to が None に入る (= 追加/削除も変化として正直に出す)。
    """
    current_map = app_health_map(current_apps)
    prev_map = app_health_map(prev_apps)
    changes = []
    for name in sorted(set(current_map) | set(prev_map)):
        before = prev_map.get(name)
        after = current_map.get(name)
        if before != after:
            changes.append({"name": name, "from": before, "to": after})
    return changes


def backup_freshness(pvc_usage, now=None):
    """pvc_usage[].backup_listing.files[].mtime の最新 1 件を鮮度付きで返す。

    returns {"namespace", "file", "mtime"(UTC datetime), "age_hours"} / 無ければ None。
    複数 namespace が listing を持っても「一番新しい 1 本」だけを見る — brief は
    「backup が生きている最小の証拠」を 1 行に畳むのが役目で、帳簿は reporter 側にある。
    未来の mtime (clock skew) は age を 0 扱いにする (負の「〜時間前」を作らない)。
    """
    best = None
    for entry in pvc_usage or []:
        if not isinstance(entry, dict):
            continue
        namespace = entry.get("namespace")
        listing = entry.get("backup_listing")
        if not isinstance(listing, dict):
            continue
        for file_entry in listing.get("files") or []:
            if not isinstance(file_entry, dict):
                continue
            mtime = parse_iso(file_entry.get("mtime"))
            if mtime is None:
                continue
            if best is None or mtime > best["mtime"]:
                best = {
                    "namespace": namespace if isinstance(namespace, str) and namespace else "unknown",
                    "file": file_entry.get("name"),
                    "mtime": mtime,
                }
    if best is None:
        return None
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif isinstance(now, str):
        now = parse_iso(now)
        if now is None:
            raise ValueError("now は aware datetime または ISO 文字列")
    age_seconds = (now - best["mtime"]).total_seconds()
    best["age_hours"] = max(0.0, age_seconds / 3600.0)
    return best


def last_json_line(raw_bytes):
    """history jsonl (複数行) の末尾から辿り、最初に JSON として読めた行を返す。

    末尾の空行・壊れた行は無視してさらに前を辿る (途中の壊れ行があっても
    直近の完全なスナップショットを「前日の最終状態」として使う)。
    1 行も読めなければ None。history は 1 行 1 スナップショットの追記専用
    ファイルなので最終行で近似する。
    """
    if not raw_bytes:
        return None
    text = raw_bytes.decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _fmt_age_hours(hours):
    """人間用の粗い鮮度表現。48 時間未満は時間、以降は日で丸める。"""
    hours = max(0.0, float(hours))
    if hours < 48.0:
        return "{}時間前".format(round(hours))
    return "{}日前".format(round(hours / 24.0))


def line_delivered(merges):
    """(a) 納品行。merges=None (取得失敗) のときは行ごと省く。"""
    if merges is None:
        return None
    total = merges.get("total")
    project = merges.get("project")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return "納品: なし (merge 0 件)"
    shown_project = project if isinstance(project, int) and not isinstance(project, bool) else 0
    return "納品: プロジェクト {} 件 / merge {} 件".format(shown_project, total)


def line_health(changes, current_apps=None, prev_available=False):
    """(b) 健全性行。current も無ければ行ごと省く。

    - 変化あり: 名前を挙げて列挙 (MAX_CHANGE_NAMES 件まで、残りは「他 N 件」)
    - 変化なし & 前日データあり: 「変化なし」
    - 前日データなし: 非 Healthy の件数だけの現在値サマリ
    片側欠損 (追加/削除) は「?」で見せる。
    """
    if current_apps is None:
        return None
    healthy_total = len(app_health_map(current_apps))
    if not changes:
        if prev_available:
            return "健全性: 変化なし ({} アプリ)".format(healthy_total)
        degraded = sum(
            1 for state in app_health_map(current_apps).values() if state != "Healthy"
        )
        if degraded:
            return "健全性: {} アプリ中 {} が非 Healthy".format(healthy_total, degraded)
        return "健全性: {} アプリすべて Healthy".format(healthy_total)

    def transition(change):
        before = change["from"] if change["from"] is not None else "?"
        after = change["to"] if change["to"] is not None else "?"
        return "{} {}→{}".format(change["name"], before, after)

    head = [transition(c) for c in changes[:MAX_CHANGE_NAMES]]
    tail = len(changes) - len(head)
    suffix = " (他 {} 件)".format(tail) if tail > 0 else ""
    return "健全性: {}".format("、".join(head)) + suffix


def line_backup(backup, stale_after_hours=DEFAULT_STALE_HOURS):
    """(c) backup 行。listing がどこにも無ければ行ごと省く。"""
    if backup is None:
        return None
    age = backup.get("age_hours")
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        return None
    namespace = backup.get("namespace") or "unknown"
    line = "backup: {} {}".format(namespace, _fmt_age_hours(age))
    if age > stale_after_hours:
        line += " (古い)"
    return line


def brief_lines(merges=None, current_apps=None, prev_apps=None, backup=None,
                stale_after_hours=DEFAULT_STALE_HOURS):
    """3 種の行情報 → 行リスト (最大 3 行)。欠けた情報源の行は省かれる。

    これが送信文の構造そのもの。「3 行を超えない」はここで構造的に担保される
    (行生成器が 3 個しか無いため)。テストで機械にも固定する。
    """
    # 前日データが無い (None)・空 ([] も同義) のときは比較不能なので変化を出さず
    # summary 表示に落とす。素の prev_apps を health_changes に渡すと全アプリが
    # 「?→X」の擬似的な新規出現に見えてしまい、障害でも無い日に誇張した変化を見せる
    prev_usable = bool(prev_apps)
    lines = [
        line_delivered(merges),
        line_health(
            health_changes(current_apps, prev_apps) if prev_usable else [],
            current_apps=current_apps,
            prev_available=prev_usable,
        ),
        line_backup(backup, stale_after_hours=stale_after_hours),
    ]
    return [line for line in lines if line]


def compose_brief(**kwargs):
    """brief_lines() を改行結合した送信文へ。全ソースが欠けた場合は空文字列。"""
    return "\n".join(brief_lines(**kwargs))


# --- ここから下は IO (unit test は触れない。--dry-run で stdout 確認) ---


def log(message):
    print("[morning-brief] {}".format(message), flush=True)


def github_get(path, token, raw=False):
    """GitHub Contents/commits API の GET。(status, body) を返す。

    raw=True は report.py get_raw_content() と同じ理由 (Contents API は 1MB 超で
    content フィールドを返さない) で生バイトを受け取る。失敗時は status だけを
    返し呼び出し側に判断させる (1 ソースの失敗で全体を止めない)。
    """
    request = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "User-Agent": "openclaw-morning-brief",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, None
    if raw:
        return 200, body
    try:
        return 200, json.loads(body) if body else None
    except json.JSONDecodeError:
        return 200, None


def fetch_sources(repo, report_ref, base_branch, day, token, now):
    """3 データ源の取得。返り値は (merges|None, current_apps|None, prev_apps|None,
    backup|None)。None は「取得できなかった」で、composer が行ごと省く。"""
    merges = None
    current_apps = None
    prev_apps = None
    backup = None

    status, raw = github_get(
        "/repos/{}/contents/ops/health/latest.json?ref={}".format(repo, report_ref),
        token, raw=True,
    )
    if status == 200:
        try:
            latest = json.loads(raw)
            current_apps = latest.get("applications")
            if not isinstance(current_apps, list):
                current_apps = None
            backup = backup_freshness(latest.get("pvc_usage"), now=now)
        except (json.JSONDecodeError, AttributeError, TypeError) as error:
            log("latest.json の解釈に失敗: {}: {}".format(type(error).__name__, error))
    else:
        log("latest.json 取得失敗: status={}".format(status))

    status, raw = github_get(
        "/repos/{}/contents/ops/health/history/{}.jsonl?ref={}".format(
            repo, day.isoformat(), report_ref),
        token, raw=True,
    )
    if status == 200:
        prev_doc = last_json_line(raw)
        if isinstance(prev_doc, dict):
            candidates = prev_doc.get("applications")
            prev_apps = candidates if isinstance(candidates, list) else None
    else:
        # 前日ファイルが無い (reporter 停止日など) は「変化の比較不能」であり、
        # 障害ではないのでログだけ残す
        log("history/{}.jsonl 取得失敗: status={}".format(day.isoformat(), status))

    day_start = datetime.datetime(day.year, day.month, day.day, tzinfo=JST)
    day_end = day_start + datetime.timedelta(days=1)
    query = urlencode({
        "sha": base_branch,
        "since": day_start.isoformat(),
        "until": day_end.isoformat(),
        "per_page": "100",
    })
    status, entries = github_get("/repos/{}/commits?{}".format(repo, query), token)
    if status == 200 and isinstance(entries, list):
        commits = [c for c in (simplify_commit(e) for e in entries) if c]
        merges = count_merges(commits, day)
        if len(entries) == 100:
            log("commits が 1 ページ上限に達した。100 件を超えた日は過小集計の可能性")
    else:
        log("commits 取得失敗: status={}".format(status))

    return merges, current_apps, prev_apps, backup


def send_telegram(token, chat_id, text):
    """sendMessage 1 通。受信系 API は一切使わない (spec「送信専用」)。"""
    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "openclaw-morning-brief",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read() or b"{}")
    if not payload.get("ok"):
        raise RuntimeError("Telegram sendMessage が ok を返さなかった: {}".format(payload))


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    github_token = os.environ.get("AUTOPILOT_GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "hikuohiku/homelab")
    report_ref = os.environ.get("REPORT_BRANCH", "ops-health-report")
    base_branch = os.environ.get("BASE_BRANCH", "main")

    # 対象日は「実行時点の JST 昨日」。CronJob は毎朝 JST 定時起動なので
    # これは常に完全に終わった 1 日になる
    now = datetime.datetime.now(datetime.timezone.utc)
    day = (now.astimezone(JST) - datetime.timedelta(days=1)).date()
    log("対象日 {} (dry_run={})".format(day, dry_run))

    if not github_token:
        raise RuntimeError("AUTOPILOT_GITHUB_TOKEN 未設定: データ源を一切読めず brief にならない")

    merges, current_apps, prev_apps, backup = fetch_sources(
        repo, report_ref, base_branch, day, github_token, now)
    text = compose_brief(
        merges=merges, current_apps=current_apps, prev_apps=prev_apps, backup=backup)

    if not text.strip():
        raise RuntimeError("読めた情報源が無く brief が空。空の挨拶を送らず失敗させる")
    log("作成: {} 行 / {} 文字".format(len(text.splitlines()), len(text)))

    if dry_run:
        print(text)
        return
    if not telegram_token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID 未設定")
    send_telegram(telegram_token, chat_id, text)
    log("Telegram へ送信した")


if __name__ == "__main__":
    main()

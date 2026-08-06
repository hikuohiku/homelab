"""ops-dashboard の配信 + 人間からの書き置きの受け口。

静的配信（fetcher が /www に置く index.html）はこれまで通り
`python3 -m http.server` と同じ SimpleHTTPRequestHandler が担う。
そこに `POST /feedback` だけを足す。

**書き置きの一次保管場所はリポジトリ内の構造化データ**（`ops-feedback` ブランチの
`ops/feedback/inbox/<id>.json`、1 件 1 ファイル）。autopilot はそれを読んで
`ops/feedback.json`（台帳）へ取り込み、状態を進める。顛末はダッシュボードに出る。
かつてここは GitHub issue #56 へコメントを投稿していたが、ダッシュボードから書いた
ものが GitHub でしか追えないうえ、状態を持てなかったので改めた（CHARTER §6）。

`main` へは直 push できない（ruleset が必須チェックを要求する）。`ops-health-report` /
`ops-dashboard` と同じ「専用ブランチへ Contents API で書く」流儀に揃える。
1 件 1 ファイルなので read-modify-write が無く、同時に書かれても衝突しない。

**この経路は autopilot に依存しない。** autopilot が死んでいても書き置きは残る
（読まれるのは復帰後になる）。

標準ライブラリのみ（イメージは python:3.14-alpine のまま、pip install しない）。
トークン (GITHUB_TOKEN) はこのプロセスの中だけで使い、応答にもページにも一切出さない。
"""

import base64
import binascii
import datetime
import html
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request

WWW_ROOT = os.environ.get("WWW_ROOT", "/www")
PORT = int(os.environ.get("PORT", "8080"))
REPO = os.environ.get("FEEDBACK_REPO", "hikuohiku/homelab")
BRANCH = os.environ.get("FEEDBACK_BRANCH", "ops-feedback")
BASE_BRANCH = os.environ.get("FEEDBACK_BASE_BRANCH", "main")
INBOX_DIR = os.environ.get("FEEDBACK_DIR", "ops/feedback/inbox").strip("/")
# 手で書く最後の手段としてだけ残す issue（CHARTER §6）。投稿先ではない
ISSUE = os.environ.get("FEEDBACK_ISSUE", "56")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
# 手元で動作確認するとき用の差し替え口（本番では設定しない）。ここを向ける先を
# 変えれば、GitHub に触らずに保存経路とエラー画面を通しで確かめられる
API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")

# ダッシュボードのフォーム（build.py の textarea maxlength）と揃える
MAX_BODY_CHARS = 20000
# リクエスト全体のバイト上限（フォーム 1 フィールドしか無いので余裕を持たせた値）
MAX_REQUEST_BYTES = 256 * 1024

ISSUE_URL = "https://github.com/{}/issues/{}".format(REPO, ISSUE)

PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body {{ font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.6;
        max-width: 46rem; margin: 3rem auto; padding: 0 1rem; }}
 .bad {{ border-left: 4px solid #c0392b; padding-left: 1rem; }}
 textarea {{ width: 100%; min-height: 12rem; }}
</style></head><body>
<div class="bad">
<h1>{title}</h1>
<p>{detail}</p>
</div>
{recovery}
<p><a href="/">ダッシュボードに戻る</a></p>
</body></html>
"""


def error_page(status, title, detail, typed_text=None):
    """失敗したことが画面で分かる形にする。

    黙って 303 で戻すと「書けたつもり」になるので、書けなかったときはエラーページを
    返す。入力した文章はそのまま返して、issue に手で貼り直せるようにする
    （ダッシュボードは 60 秒ごとに差し替わるため、戻ると入力は消える）。
    issue #56 は autopilot が今も読む経路なので、退避先として有効
    （ただし台帳に載るのは autopilot が取り込んだときで、そこは同じ）。
    """
    recovery = ""
    if typed_text:
        recovery = (
            "<p>入力した内容は失われていない。下をコピーして "
            '<a href="{url}">issue #{issue}</a> に直接書けば、autopilot は同じように読む。</p>'
            "<textarea readonly>{text}</textarea>"
        ).format(url=ISSUE_URL, issue=ISSUE, text=html.escape(typed_text))
    body = PAGE.format(
        title=html.escape(title), detail=html.escape(detail), recovery=recovery
    ).encode("utf-8")
    return status, body


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def new_note_id(when=None):
    """`F-20260806T121100Z-3f9a1c`。時刻でソートでき、同時投稿でも衝突しない。

    連番にしないのは、採番のために台帳を読んで書き戻す（read-modify-write）ことに
    なり、同時に 2 件書かれたときに片方が消えるため。id は書いた時点で確定し、
    台帳に取り込まれた後も付け替えない。
    """
    when = when or now_utc()
    rand = binascii.hexlify(os.urandom(3)).decode()
    return "F-{}-{}".format(when.strftime("%Y%m%dT%H%M%SZ"), rand)


def github_request(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "homelab-ops-dashboard",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001 — GitHub が JSON を返さないこともある
            return e.code, {"message": raw.decode("utf-8", errors="replace")[:500]}


def api_message(payload):
    return str(payload.get("message", ""))[:300] if isinstance(payload, dict) else ""


def ensure_branch():
    """書き込み先ブランチが無ければ main から作る（ops-health-reporter と同型）。"""
    status, payload = github_request("GET", "/repos/{}/git/ref/heads/{}".format(REPO, BRANCH))
    if status == 200:
        return
    if status != 404:
        raise RuntimeError("ブランチ {} の確認に失敗: HTTP {} {}".format(BRANCH, status, api_message(payload)))
    status, base = github_request("GET", "/repos/{}/git/ref/heads/{}".format(REPO, BASE_BRANCH))
    if status != 200:
        raise RuntimeError("{} の ref 取得に失敗: HTTP {} {}".format(BASE_BRANCH, status, api_message(base)))
    status, payload = github_request(
        "POST",
        "/repos/{}/git/refs".format(REPO),
        {"ref": "refs/heads/{}".format(BRANCH), "sha": base["object"]["sha"]},
    )
    if status not in (200, 201):
        raise RuntimeError("ブランチ {} の作成に失敗: HTTP {} {}".format(BRANCH, status, api_message(payload)))


def put_note(note):
    """書き置き 1 件を新しいファイルとして作る。(status, payload, path) を返す。

    `sha` を渡さない＝新規作成のみ。既存ファイルを上書きすることは無い
    （同じ id が既にあれば GitHub が 422 で断る。書き置きを取り違えて消さない）。
    """
    path = "{}/{}.json".format(INBOX_DIR, note["id"])
    content = json.dumps(note, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    status, payload = github_request(
        "PUT",
        "/repos/{}/contents/{}".format(REPO, path),
        {
            "message": "feedback {} ({})".format(note["id"], note["source"]),
            "content": base64.b64encode(content.encode("utf-8")).decode(),
            "branch": BRANCH,
        },
    )
    return status, payload, path


def submit(text):
    """書き置きを保存する。成功したら id を返す。失敗は RuntimeError。"""
    ensure_branch()
    for attempt in range(2):
        note = {
            "id": new_note_id(),
            "source": "ops-dashboard",
            "received": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "body": text,
        }
        status, payload, path = put_note(note)
        if status in (200, 201):
            sys.stderr.write("feedback: wrote {} ({} chars) to {}\n".format(path, len(text), BRANCH))
            return note["id"]
        # 422 = 同じパスが既にある（乱数衝突）。id を振り直して 1 度だけやり直す
        if status == 422 and attempt == 0:
            continue
        raise RuntimeError("HTTP {}{}".format(
            status, ("（" + api_message(payload) + "）") if api_message(payload) else ""))
    raise RuntimeError("id が衝突し続けた")


class Handler(http.server.SimpleHTTPRequestHandler):
    # 静的配信の挙動（パス解決・ディレクトリトラバーサル対策）は親のまま使い、
    # POST だけを足す。translate_path は上書きしない
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW_ROOT, **kwargs)

    def _respond(self, status, body, headers=None):
        self.send_response(status)
        for k, v in (headers or {"Content-Type": "text/html; charset=utf-8"}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _fail(self, status, title, detail, typed_text=None):
        status, body = error_page(status, title, detail, typed_text)
        self._respond(status, body)

    def do_POST(self):  # noqa: N802 — http.server の命名規約
        if urllib.parse.urlparse(self.path).path != "/feedback":
            self._fail(404, "そのようなページは無い", "POST を受けるのは /feedback だけ。")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0:
            self._fail(400, "本文が空", "フォームの内容が届かなかった。")
            return
        if length > MAX_REQUEST_BYTES:
            self._fail(413, "入力が大きすぎる", "送信できるのは 256 KiB まで。")
            return

        raw = self.rfile.read(length)
        # 素の form encoding しか受けない。JSON など他の形は解釈しない
        fields = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
        text = (fields.get("body") or [""])[0]
        # フォームは CRLF で送ってくる。改行を素直な形へ寄せ、制御文字（NUL 等）は落とす
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(c for c in text if c in "\n\t" or ord(c) >= 0x20)
        text = text.strip()

        if not text:
            self._fail(400, "本文が空", "何も書かれていないので保存しなかった。")
            return
        if len(text) > MAX_BODY_CHARS:
            # 黙って切ると指示の後半が消えたことに気づけない。断って人間に分割させる
            self._fail(
                413,
                "本文が長すぎる",
                "{} 文字あった。{} 文字までにするか、分けて投稿してほしい。".format(
                    len(text), MAX_BODY_CHARS
                ),
                text,
            )
            return

        if not TOKEN:
            self._fail(
                503,
                "保存できない（トークン未設定）",
                "GITHUB_TOKEN がコンテナに渡っていない。ExternalSecret "
                "'ops-dashboard-github-token' と Doppler の AUTOPILOT_GITHUB_TOKEN を確認する。",
                text,
            )
            return

        try:
            note_id = submit(text)
        except RuntimeError as e:
            sys.stderr.write("feedback: github rejected: {}\n".format(e))
            self._fail(502, "GitHub が受け付けなかった", str(e), text)
            return
        except Exception as e:  # noqa: BLE001 — 到達不能・TLS・タイムアウト等
            sys.stderr.write("feedback: write failed: {}: {}\n".format(type(e).__name__, e))
            self._fail(502, "GitHub に届かなかった", "{}: {}".format(type(e).__name__, e), text)
            return

        # 成功時だけリダイレクト。クエリはダッシュボード側が「預かった」表示に使う
        # （build.py の既存の JS は feedback=ok を含むかだけを見るので id を足しても壊れない）
        self._respond(
            303,
            b"",
            {
                "Location": "/?feedback=ok&id=" + urllib.parse.quote(note_id),
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

    def log_message(self, fmt, *args):
        # 本文はログに出さない（書き置きの中身をログへ二重に残さない）
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # GitHub API 呼び出しの間も静的配信と probe を止めない（単一スレッドだと
    # 遅い POST が liveness probe を巻き添えにして Pod ごと再起動させる）
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    sys.stderr.write(
        "ops-dashboard: serving {} on :{} (feedback -> {}@{}:{}/, token={})\n".format(
            WWW_ROOT, PORT, REPO, BRANCH, INBOX_DIR, "set" if TOKEN else "MISSING"
        )
    )
    Server(("", PORT), Handler).serve_forever()

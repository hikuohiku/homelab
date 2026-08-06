"""ops-dashboard の配信 + ダッシュボードからの指示投稿口。

静的配信（fetcher が /www に置く index.html）はこれまで通り
`python3 -m http.server` と同じ SimpleHTTPRequestHandler が担う。
そこに `POST /feedback` だけを足し、フォームの本文を GitHub issue
（人間からのフィードバック窓口。CHARTER §6）へコメントとして投稿する。

新しい protocol は作らない。autopilot は今まで通り issue のコメントだけを読む。

標準ライブラリのみ（イメージは python:3.14-alpine のまま、pip install しない）。
トークン (GITHUB_TOKEN) はこのプロセスの中だけで使い、応答にもページにも一切出さない。
"""

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
ISSUE = os.environ.get("FEEDBACK_ISSUE", "56")
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# issue コメント 1 件の上限は 65536 文字。手前で切って、超えたら黙って捨てずに断る
MAX_BODY_CHARS = 20000
# リクエスト全体のバイト上限（フォーム 1 フィールドしか無いので余裕を持たせた値）
MAX_REQUEST_BYTES = 256 * 1024

ISSUE_URL = "https://github.com/{}/issues/{}".format(REPO, ISSUE)

# autopilot がコメントの出所を機械的に判別できるようにする印。
# これより下がフォームに入力された本文そのもの。
MARKER = "<!-- source: ops-dashboard -->"
HEADER = (
    MARKER + "\n"
    + "**ops-dashboard から投稿**（tailnet 内のダッシュボード画面のフォーム経由。"
    + "投稿者の個人認証はしていない）\n\n---\n\n"
)

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

    黙って 303 で戻すと「書けたつもり」になるので、投稿できなかったときは
    エラーページを返す。入力した文章はそのまま返して、GitHub に手で貼り直せるようにする
    （ダッシュボードは 60 秒ごとに差し替わるため、戻ると入力は消える）。
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


def post_comment(text):
    """issue にコメントを 1 件作る。(status, payload) を返す。"""
    payload = json.dumps({"body": HEADER + text}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/repos/{}/issues/{}/comments".format(REPO, ISSUE),
        data=payload,
        method="POST",
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
        # フォームは CRLF で送ってくる。issue に素直に載る形へ寄せ、
        # 制御文字（NUL 等）は落とす
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "".join(c for c in text if c in "\n\t" or ord(c) >= 0x20)
        text = text.strip()

        if not text:
            self._fail(400, "本文が空", "何も書かれていないので投稿しなかった。")
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
                "投稿できない（トークン未設定）",
                "GITHUB_TOKEN がコンテナに渡っていない。ExternalSecret "
                "'ops-dashboard-github-token' と Doppler の AUTOPILOT_GITHUB_TOKEN を確認する。",
                text,
            )
            return

        try:
            status, payload = post_comment(text)
        except Exception as e:  # noqa: BLE001 — 到達不能・TLS・タイムアウト等
            sys.stderr.write("feedback: post failed: {}: {}\n".format(type(e).__name__, e))
            self._fail(
                502,
                "GitHub に届かなかった",
                "{}: {}".format(type(e).__name__, e),
                text,
            )
            return

        if status != 201:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message", ""))[:300]
            sys.stderr.write("feedback: github returned {} {}\n".format(status, message))
            self._fail(
                502,
                "GitHub が受け付けなかった",
                "HTTP {}{}".format(status, ("（" + message + "）") if message else ""),
                text,
            )
            return

        sys.stderr.write("feedback: posted {} chars to issue #{}\n".format(len(text), ISSUE))
        # 成功時だけリダイレクト。クエリはダッシュボード側が「投稿できた」表示に使える
        self._respond(
            303,
            b"",
            {"Location": "/?feedback=ok", "Content-Type": "text/plain; charset=utf-8"},
        )

    def log_message(self, fmt, *args):
        # 本文はログに出さない（指示の中身をログへ二重に残さない）
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # GitHub API 呼び出しの間も静的配信と probe を止めない（単一スレッドだと
    # 遅い POST が liveness probe を巻き添えにして Pod ごと再起動させる）
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    sys.stderr.write(
        "ops-dashboard: serving {} on :{} (feedback -> {} #{}, token={})\n".format(
            WWW_ROOT, PORT, REPO, ISSUE, "set" if TOKEN else "MISSING"
        )
    )
    Server(("", PORT), Handler).serve_forever()

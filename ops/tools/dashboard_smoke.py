#!/usr/bin/env python3
"""Mission Control の headless 描画スモーク (P-0193)。

ダッシュボードは Next.js のクライアント描画で、readiness probe が見るのは
「HTTP 200 が返る」ことだけ。critic (08-22) が人間の目で発見した
「鼓動しています(緑) の隣に異常終了(赤)」のような、実際に描画したときだけ
見える嘘 (JS エラー・API 失敗時の白画面・矛盾シグナルの共存・古いデータ) を
機械が毎日先に見つけるための検眼装置。

使い方 (クラスタ内の CronJob からは Service DNS を既定で使う):

    python3 ops/tools/dashboard_smoke.py \
      --out smoke-result.json --screenshot smoke-result.png

    # クラスタ外から (tailnet 経由):
    python3 ops/tools/dashboard_smoke.py \
      --url https://ops-dashboard.<tailnet>.ts.net/ \
      --out smoke-result.json --screenshot smoke-result.png

断言するもの (DoD):
  (a) HTTP 200 とレンダリング完了 (.loading スピナの消滅を壁時計で待つ)
  (b) 主要セクションの存在 — 鼓動チップ・ライブ領域・プロジェクトボード
      (ボードは既定ビューの裏なので nav を JS でクリックしてから再取得)
  (c) 明示的矛盾 — critic 08-22 指摘の形状。「正常」鼓動チップと
      global-warning / HEART SIGNAL LOST / 要確認チップの共存、
      「正常」なのに LAST HEART 観測なし、心拍表示の古さ

実装メモ — 実測済みの罠 (2026-08-23, chromium 151):
  - `--dump-dom` は load 時点でダンプするため snapshot fetch 前の生 HTML になる。
    `--timeout=N` でも待たない (上限値であり固定待ちではない)
  - `--virtual-time-budget` は EventSource (transcript SSE) や 10 秒ポーリングの
    /api/snapshot が仮想時間を滞留させ、実サイトでは空出力またはハングした
  - そこで CDP (--remote-debugging-pipe, WebSocket 不要) で操作し、
    Runtime.evaluate のポーリングで描画完了を壁時計で待つ。
    パイプの fd 向きは実測で確定済み: **子プロセスがコマンドを fd3 から読み、
    応答を fd4 へ書く** (chrome_main_delegate.cc は起動時に両 fd のオープンを要求)

既知の死角 (伏せずに書き残る):
  - 描画対象の DOM 断言は class 名 / 文言に依存する。apps/ops-dashboard/app/src/app/page.tsx
    の書き換えで文言が変わると誤検知 (fail) になる。それは装置が正しく鳴っている状態なので、
    鳴ったらこのファイルと page.tsx の両方を直すこと
  - 要対応キュー (view=attention) は断言しない。v1 の守備範囲は DoD の
    鼓動・プロジェクト一覧のみ
  - HTTP status は SSR シェルへの GET で確認する。chromium の内部リソース
    (JS chunk 等) の 404 は見えない — それで壊れるなら .loading が消えず
    render-complete が鳴る

判定ロジックの固定テスト: ops/tests/test_dashboard_smoke.py (別 PR で足す)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

# クラスタ内 CronJob 既定。クラスタ外からは --url で tailnet URL を渡す
DEFAULT_URL = "http://ops-dashboard.autopilot.svc"
DEFAULT_CHROMIUM = "/usr/bin/chromium"
DEFAULT_LOAD_WAIT_S = 20.0   # Page.loadEventFired 待ちの壁時計上限
DEFAULT_RENDER_WAIT_S = 30.0  # .loading 消滅待ちの壁時計上限 (snapshot fetch + hydration)
# 心拍表示の鮮度しきい値。UI 側の stale 判定は 5 分 (page.tsx)、health reporter の
# 集計間隔は約 30 分。日次ジョブとしては「半日以上古い」を異常としたいが、
# UI が stale=True を要確認チップとして既に出すので、ここではその補助線として
# 15 分に置いた (--max-heart-age-s で変える)
DEFAULT_MAX_HEART_AGE_S = 900.0
# 白画面でないことの下限。実ページの可視テキスト実測 (~1000 文字以上) に対して
# 十分小さく、完全な白画面や JS 壊滅とは判別できる値
MIN_VISIBLE_TEXT_CHARS = 200

PASS, FAIL = "pass", "fail"

JST = datetime.timezone(datetime.timedelta(hours=9), name="JST")  # 日本は DST 無し

HEART_SIGNAL_LOST_MARK = "HEART SIGNAL LOST"
LOADING_MARK = "管制信号を同期中"
MASTHEAD_MARK = "MISSION CONTROL"
EMPTY_SHIFT_MARK = "走行中のエージェントはありません"
LIVE_TRANSCRIPT_MARK = "LIVE TRANSCRIPT"
BOARD_TITLE_MARK = "プロジェクトボード"
NO_OBSERVATION_LABEL = "観測なし"


def make_result(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


# ---------------------------------------------------------------------------
# 抽出層: DOM 文字列 → 断言の材料 (純関数)
# ---------------------------------------------------------------------------

class _VisibleText(HTMLParser):
    """可視テキストだけ集める。script/style の中身は Next.js flight data
    (__next_f.push(...)) が混ざるので除外する。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def visible_text(html):
    parser = _VisibleText()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — 断言の材料集めで死なない。壊れたら短いまま落ちる
        pass
    return " ".join(parser.parts)


def find_heart_chips(html):
    """heart-chip 要素の HTML 断片を返す。

    React 出力は class="heart-chip " / class="heart-chip heart-chip--bad" のように
    修飾子の有無だけの差なので [^"]* で受ける (2026-08-23 実測: class="heart-chip ")。
    チップ内に div をネストしない構造を page.tsx が保っている前提で .*?</div> まで。
    """
    return re.findall(r'<div class="[^"]*heart-chip[^"]*"[^>]*>.*?</div>', html, re.S)


def chip_is_ok(chunk):
    return ">正常<" in chunk and "--bad" not in chunk


def chip_is_bad(chunk):
    return "--bad" in chunk or ">要確認<" in chunk


def global_warning_texts(html):
    inner_list = re.findall(r'<div class="global-warning"[^>]*>(.*?)</div>', html, re.S)
    return [visible_text(inner) for inner in inner_list]


def last_heart_label(html):
    """status-line の LAST HEART 直後の文言 ("MM/DD HH:MM:SS" or "観測なし")。"""
    m = re.search(r"LAST HEART\s+([^<\n]+)", html)
    return m.group(1).strip() if m else None


def parse_jst_stamp(label, now_utc):
    """formatDate() の出力 ("08/23 21:37:26", JST) を aware datetime へ。

    年は画面に出ないため現在年を仮置きし、未来に跳んでいたら前年と直す
    (年末年始)。解析できないなら None (呼び側で unknown/fail に落とす)。
    """
    label = (label or "").strip()
    try:
        now_jst = now_utc.astimezone(JST)
        # 年を先に結合してから解釈する (%m/%d 単独は閏日で曖昧になるため。Python 3.14 警告)
        parsed = datetime.datetime.strptime(
            f"{now_jst.year}/{label}", "%Y/%m/%d %H:%M:%S"
        ).replace(tzinfo=JST)
        if (parsed - now_jst).total_seconds() > 2 * 86400:
            parsed = parsed.replace(year=now_jst.year - 1)
        return parsed
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 判定層: DOM → 検査結果リスト (純関数。unittest で両方向固定する対象)
# ---------------------------------------------------------------------------

def check_rendering(html):
    text = visible_text(html)
    results = []
    results.append(make_result(
        "rendered-masthead",
        PASS if MASTHEAD_MARK in text else FAIL,
        "masthead の MISSION CONTROL 表示" if MASTHEAD_MARK in text
        else "MISSION CONTROL が描画されていない (JS エラーか白画面)",
    ))
    loading_gone = LOADING_MARK not in html
    results.append(make_result(
        "render-complete",
        PASS if loading_gone else FAIL,
        ".loading スピナ消滅 (snapshot fetch 後の描画完了)" if loading_gone
        else f"『{LOADING_MARK}』が残っている — snapshot 取得または hydration が完了していない",
    ))
    blank = len(text.strip()) >= MIN_VISIBLE_TEXT_CHARS
    results.append(make_result(
        "non-blank",
        PASS if blank else FAIL,
        f"可視テキスト {len(text.strip())} 文字 (>= {MIN_VISIBLE_TEXT_CHARS})" if blank
        else f"可視テキスト {len(text.strip())} 文字しか無い (白画面の疑い)",
    ))
    return results


def check_sections(html):
    """既定ビュー (live) の主要セクション。ボードは裏ビューなので
    check_project_board() が nav クリック後の DOM を受け持つ。"""
    text = visible_text(html)
    chips = find_heart_chips(html)
    results = []
    has_chip = bool(chips) and any("HEART / BEAT" in c for c in chips)
    results.append(make_result(
        "section-heartbeat",
        PASS if has_chip else FAIL,
        f"鼓動チップ x{len(chips)}" if has_chip
        else "鼓動チップ (heart-chip) が描画されていない",
    ))
    live_ok = LIVE_TRANSCRIPT_MARK in html or EMPTY_SHIFT_MARK in text
    results.append(make_result(
        "section-live-area",
        PASS if live_ok else FAIL,
        "ライブ領域 (transcript または静穏表示)" if live_ok
        else "ライブ領域が描画されていない",
    ))
    return results


def check_project_board(dom):
    """nav クリック後のプロジェクトビュー DOM を断言する (DoD b の『プロジェクト一覧』)。"""
    ok = BOARD_TITLE_MARK in visible_text(dom)
    return [make_result(
        "section-project-board",
        PASS if ok else FAIL,
        "プロジェクトボードが描画された" if ok
        else "nav クリック後にプロジェクトボードが描画されない (遷移またはボード描画の破綻)",
    )]


def check_contradictions(html):
    """critic 08-22 指摘の形状: 「緑の正常表示」と「赤の異常表示」の共存。

    現行 UI に旧ダッシュボードの「異常終了(赤)」タイルは無い (status-line は
    LAST HEART 時刻のみ)。そのため形状を一般化し、「正常チップ」と
    (global-warning | HEART SIGNAL LOST | 要確認チップ | 観測なし) の共存を
    矛盾として落とす。どちらが嘘かまで装置は判定しない — 共存を見つけたら
    人間の目で確かめるために鳴らすのが役目。
    """
    chips = find_heart_chips(html)
    ok_chips = [c for c in chips if chip_is_ok(c)]
    bad_chips = [c for c in chips if chip_is_bad(c)]
    warnings = global_warning_texts(html)
    signal_lost = HEART_SIGNAL_LOST_MARK in html
    label = last_heart_label(html)
    results = []

    reds = ([f"global-warning: {w}" for w in warnings]
            + ([HEART_SIGNAL_LOST_MARK] if signal_lost else []))
    if ok_chips and reds:
        results.append(make_result(
            "no-lie-coexistence", FAIL,
            "正常チップと異常表示が共存: " + "; ".join(reds)))
    elif ok_chips:
        results.append(make_result("no-lie-coexistence", PASS, "正常チップ単独"))
    else:
        results.append(make_result(
            "no-lie-coexistence", PASS,
            "正常チップが無いので共存検査の対象外 (chip 自体の有無は section-heartbeat 参照)"))

    if ok_chips and bad_chips:
        results.append(make_result(
            "no-mixed-heart-signals", FAIL,
            f"正常チップ x{len(ok_chips)} と要確認チップ x{len(bad_chips)} が同時に存在 "
            "(critic 08-22 の『緑の隣に赤』そのもの)"))
    elif chips:
        results.append(make_result(
            "no-mixed-heart-signals", PASS,
            f"チップは一貫した状態 ({len(chips)} 個)"))
    else:
        results.append(make_result("no-mixed-heart-signals", PASS, "チップ無しのため対象外"))

    unobserved = label == NO_OBSERVATION_LABEL
    if ok_chips and unobserved:
        results.append(make_result(
            "no-unobserved-pulse", FAIL,
            "LAST HEART が『観測なし』なのに鼓動が正常表示されている"))
    else:
        results.append(make_result(
            "no-unobserved-pulse", PASS,
            f"LAST HEART={label!r}" if label else "LAST HEART 表示が見つからない"))

    return results


def check_freshness(html, now_utc, max_age_s=DEFAULT_MAX_HEART_AGE_S):
    label = last_heart_label(html)
    if label is None:
        return [make_result("heartbeat-fresh", FAIL,
                            "LAST HEART 表示自体が見つからない")]
    parsed = parse_jst_stamp(label, now_utc)
    if parsed is None:
        return [make_result("heartbeat-fresh", FAIL,
                            f"LAST HEART の時刻を解釈できない: {label!r}")]
    age_s = (now_utc - parsed).total_seconds()
    if age_s > max_age_s:
        return [make_result(
            "heartbeat-fresh", FAIL,
            f"LAST HEART が古い: {label} JST ({int(age_s)} 秒前 > 上限 {int(max_age_s)} 秒)")]
    return [make_result("heartbeat-fresh", PASS,
                        f"LAST HEART {label} JST ({int(age_s)} 秒前)")]


def evaluate_dom(html, *, now_utc, max_heart_age_s=DEFAULT_MAX_HEART_AGE_S):
    """既定ビュー DOM への全検査。{"checks": [...], "ok": bool} を返す (集約点)。

    プロジェクトボード (裏ビュー) は別 DOM なので含まない — run_smoke が
    check_project_board() を追加で走らせる。unittest はこの関数を両方向で固定する。
    """
    checks = (
        check_rendering(html)
        + check_sections(html)
        + check_contradictions(html)
        + check_freshness(html, now_utc, max_heart_age_s)
    )
    return {"checks": checks, "ok": all(c["status"] != FAIL for c in checks)}


# ---------------------------------------------------------------------------
# CDP クライアント: chromium --remote-debugging-pipe (標準ライブラリのみ)
# ---------------------------------------------------------------------------

class CdpError(RuntimeError):
    pass


class CdpPipeSession:
    """--remote-debugging-pipe での最小限の CDP クライアント。

    メッセージは NUL 区切り JSON。fd 向きは本環境 chromium 151 の実測で
    子が読み cmd=3 / 書き res=4 (モジュール docstring 参照)。
    """

    def __init__(self, proc, cmd_write_fd, res_read_fd):
        self.proc = proc
        self.cmd_fd = cmd_write_fd
        self.res_fd = res_read_fd
        self._buf = b""
        self._next_id = 0

    def send(self, method, params=None, session_id=None):
        self._next_id += 1
        msg = {"id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        os.write(self.cmd_fd, json.dumps(msg).encode() + b"\0")
        return self._next_id

    def recv(self, deadline_monotonic):
        while True:
            while b"\0" in self._buf:
                line, self._buf = self._buf.split(b"\0", 1)
                if line.strip():
                    return json.loads(line)
            remain = deadline_monotonic - time.monotonic()
            if remain <= 0:
                raise TimeoutError(f"CDP応答タイムアウト: {self._buf[:120]!r}")
            r, _, _ = select.select([self.res_fd], [], [], min(remain, 1.0))
            if not r:
                continue
            chunk = os.read(self.res_fd, 65536)
            if not chunk:
                raise EOFError("chromium がデバッグパイプを閉じた")
            self._buf += chunk

    def wait_response(self, want_id, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            msg = self.recv(deadline)
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise CdpError(f"{msg.get('error')}")
                return msg.get("result", {})

    def wait_event(self, method, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            msg = self.recv(deadline)
            if msg.get("method") == method:
                return msg.get("params", {})

    def evaluate(self, expression, session_id, timeout_s=15.0):
        mid = self.send("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True},
                        session_id=session_id)
        result = self.wait_response(mid, timeout_s)
        if result.get("exceptionDetails"):
            raise CdpError(f"evaluate 失敗: {result['exceptionDetails'].get('exception', {}).get('description', '')[:300]}")
        return (result.get("result") or {}).get("value")

    def close(self):
        for fd in (self.cmd_fd, self.res_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def spawn_chromium(chromium_bin, user_data_dir):
    """CDP パイプ付きで chromium を起動し (proc, session) を返す。"""
    to_child_r, to_child_w = os.pipe()   # コマンド: 親が書く → 子が fd3 で読む
    from_child_r, from_child_w = os.pipe()  # 応答: 子が fd4 で書く → 親が読む

    def _setup_child_fds():
        os.dup2(to_child_r, 3)
        os.dup2(from_child_w, 4)

    argv = [
        chromium_bin,
        "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-pipe", "about:blank",
    ]
    proc = subprocess.Popen(
        argv, preexec_fn=_setup_child_fds,
        pass_fds=(to_child_r, from_child_w, 3, 4),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 親側で子側の端を閉じる
    os.close(to_child_r)
    os.close(from_child_w)
    session = CdpPipeSession(proc, to_child_w, from_child_r)
    return proc, session


def attach_page(session, timeout_s):
    """初期ターゲット (type=page) に attach して sessionId を返す。

    background_page (内蔵拡張) も列挙されるので type で選ぶ。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        mid = session.send("Target.getTargets")
        infos = session.wait_response(mid, max(1.0, deadline - time.monotonic())).get("targetInfos", [])
        pages = [t for t in infos if t.get("type") == "page"]
        if pages:
            break
        if time.monotonic() > deadline:
            raise CdpError(f"type=page のターゲットが現れない: {infos}")
        time.sleep(0.2)
    mid = session.send("Target.attachToTarget",
                       {"targetId": pages[0]["targetId"], "flatten": True})
    return session.wait_response(mid, timeout_s)["sessionId"]


def navigate_and_wait_rendered(session, session_id, url, *, load_wait_s, render_wait_s):
    """遷移し、loadEventFired → .loading 消滅 (壁時計ポーリング) を待つ。"""
    session.send("Page.enable", session_id=session_id)
    mid = session.send("Page.navigate", {"url": url}, session_id=session_id)
    session.wait_response(mid, load_wait_s)
    session.wait_event("Page.loadEventFired", load_wait_s)
    deadline = time.monotonic() + render_wait_s
    while True:
        gone = session.evaluate("!document.querySelector('.loading')", session_id)
        if gone is True:
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)


def click_nav_by_text(session, session_id, label):
    """nav 内のボタンを文言で探して click() する。見つからなければ False。"""
    found = session.evaluate(
        f"!![...document.querySelectorAll('nav button')]"
        f".find(b => b.textContent.includes('{label}'))",
        session_id)
    if not found:
        return False
    session.evaluate(
        f"[...document.querySelectorAll('nav button')]"
        f".find(b => b.textContent.includes('{label}')).click()",
        session_id)
    return True


def capture_dom_and_screenshot(session, session_id):
    dom = session.evaluate("document.documentElement.outerHTML", session_id, timeout_s=30.0)
    if not isinstance(dom, str) or not dom:
        raise CdpError("outerHTML の取得が空")
    mid = session.send("Page.captureScreenshot",
                       {"format": "png", "captureBeyondViewport": True},
                       session_id=session_id)
    data = session.wait_response(mid, 30.0).get("data")
    if not data:
        raise CdpError("スクリーンショットの取得が空")
    return dom, base64.b64decode(data)


def shutdown(proc, session):
    """Browser.close を試み、応答なくても回収する。"""
    try:
        session.send("Browser.close")
    except (OSError, BrokenPipeError):
        pass
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                session.recv(deadline)
            except (TimeoutError, EOFError):
                break
    finally:
        session.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# オーケストレーション
# ---------------------------------------------------------------------------

def http_status(url, timeout_s=15.0):
    """SSR シェルへの GET で HTTP status を取る (DoD a の前半)。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ops-dashboard-smoke"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return resp.status, resp.read(256)


def run_smoke(url, *, chromium_bin=DEFAULT_CHROMIUM,
              load_wait_s=DEFAULT_LOAD_WAIT_S, render_wait_s=DEFAULT_RENDER_WAIT_S,
              max_heart_age_s=DEFAULT_MAX_HEART_AGE_S, screenshot_bytes_sink=None):
    """本体。result dict を返す (exit code は main で決める)。

    tool/environment エラー (chromium 無し・到達不能等) は例外で上げる —
    「ページの嘘」(rc=1) と「装置が回らない」(rc=2) を分けるため。
    """
    started = time.monotonic()

    def _finish(res):
        res["elapsed_s"] = round(time.monotonic() - started, 2)
        res["failed_checks"] = [c["name"] for c in res["checks"] if c["status"] == FAIL]
        res["ok"] = not res["failed_checks"]
        return res

    result = {
        "schema": 1,
        "tool": "dashboard_smoke",
        "project": "P-0193",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": url,
        "http_status": None,
        "checks": [],
        "failed_checks": [],
        "ok": False,
    }

    try:
        status, _head = http_status(url)
    except urllib.error.HTTPError as e:
        result["http_status"] = e.code
        result["checks"].append(make_result("http-status", FAIL, f"HTTP {e.code}"))
        return _finish(result)
    result["http_status"] = status
    result["checks"].append(make_result(
        "http-status", PASS if status == 200 else FAIL, f"HTTP {status}"))
    if status != 200:
        return _finish(result)

    user_data_dir = tempfile.mkdtemp(prefix="dashboard-smoke-")
    proc = None
    session = None
    try:
        proc, session = spawn_chromium(chromium_bin, user_data_dir)
        sid = attach_page(session, timeout_s=15.0)
        rendered = navigate_and_wait_rendered(
            session, sid, url, load_wait_s=load_wait_s, render_wait_s=render_wait_s)
        result["checks"].append(make_result(
            "render-complete-flow",
            PASS if rendered else FAIL,
            ".loading が消えた" if rendered else
            f"{render_wait_s} 秒以内に描画が完了しない (.loading 残置)"))
        # 完了の有無にかかわらず現状の DOM と PNG を記録する (失敗時は原因の手がかり)
        dom_live, png = capture_dom_and_screenshot(session, sid)
        result["dom_bytes"] = len(dom_live)
        if screenshot_bytes_sink is not None:
            screenshot_bytes_sink.write(png)
        if not rendered:
            return _finish(result)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        result["checks"].extend(
            evaluate_dom(dom_live, now_utc=now_utc, max_heart_age_s=max_heart_age_s)["checks"])

        # プロジェクトボードは既定ビューの裏にあるため、nav をクリックして描画させる
        # (DoD b の「プロジェクト一覧」。page.tsx が view state の URL 対応をするまで)
        if click_nav_by_text(session, sid, "プロジェクト"):
            deadline = time.monotonic() + render_wait_s
            board_ok = False
            while time.monotonic() < deadline:
                if session.evaluate("!!document.querySelector('.board')", sid) is True:
                    board_ok = True
                    break
                time.sleep(0.5)
            if board_ok:
                dom_board, _ = capture_dom_and_screenshot(session, sid)
                result["checks"].extend(check_project_board(dom_board))
                result["dom_project_board_bytes"] = len(dom_board)
            else:
                result["checks"].append(make_result(
                    "section-project-board", FAIL,
                    f"nav クリック後 {render_wait_s} 秒で .board が出現しない"))
        else:
            result["checks"].append(make_result(
                "section-project-board", FAIL,
                "nav に『プロジェクト』ボタンが見つからない"))
    finally:
        if session is not None or proc is not None:
            shutdown(proc, session)
        shutil.rmtree(user_data_dir, ignore_errors=True)

    if screenshot_bytes_sink is not None and screenshot_bytes_sink.done():
        result["screenshot"] = {
            "path": str(screenshot_bytes_sink.path),
            "bytes": screenshot_bytes_sink.bytes,
            "sha256": screenshot_bytes_sink.sha256,
            "view": "live",
        }
    return _finish(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Sink:
    """スクリーンショットを 1 回だけ受け取る小さな受け皿 (失敗経路との共用)。
    受け取った実体の大きさとダイジェストを記録し、結果 JSON に載せる。"""

    def __init__(self, path):
        self.path = path
        self._written = False
        self.bytes = 0
        self.sha256 = None

    def done(self):
        return self._written

    def write(self, payload: bytes):
        if self._written or not self.path:
            return
        with open(self.path, "wb") as f:
            f.write(payload)
        self._written = True
        self.bytes = len(payload)
        self.sha256 = hashlib.sha256(payload).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dashboard_smoke.py",
        description="Mission Control の headless 描画スモーク (P-0193)")
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"描画対象 (既定: {DEFAULT_URL})")
    parser.add_argument("--chromium-bin", default=DEFAULT_CHROMIUM)
    parser.add_argument("--load-wait-s", type=float, default=DEFAULT_LOAD_WAIT_S)
    parser.add_argument("--render-wait-s", type=float, default=DEFAULT_RENDER_WAIT_S)
    parser.add_argument("--max-heart-age-s", type=float, default=DEFAULT_MAX_HEART_AGE_S,
                        help="LAST HEART の許容経過秒 (既定 900)")
    parser.add_argument("--out", help="結果 JSON の書き先 (未指定は stdout)")
    parser.add_argument("--screenshot", help="PNG の書き先 (未指定は保存しない)")
    args = parser.parse_args(argv)

    sink = _Sink(args.screenshot) if args.screenshot else None
    try:
        result = run_smoke(
            args.url, chromium_bin=args.chromium_bin,
            load_wait_s=args.load_wait_s, render_wait_s=args.render_wait_s,
            max_heart_age_s=args.max_heart_age_s,
            screenshot_bytes_sink=sink)
    except FileNotFoundError as e:
        print(f"chromium を起動できない: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — 装置自身の故障は rc=2 で「ページの嘘」と区別する
        print(f"smoke 実行に失敗 (tool/environment error): {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print(f"結果を {args.out} に書きました (ok={result['ok']})")
    else:
        print(payload)
    marks = {"pass": "[合格]", "fail": "[不合格]"}
    for c in result["checks"]:
        print(f" {marks[c['status']]} {c['name']}: {c['detail']}")
    if result["ok"]:
        print(" 判定: 合格 — ダッシュボードは嘘をついていない")
        return 0
    print(f" 判定: 不合格 — {', '.join(result['failed_checks'])}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

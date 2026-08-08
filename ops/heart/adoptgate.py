"""採択ゲート — 予告の前に、使い捨ての新品 clone で verify を実測する。

なぜ在るか (P-0015):
  spec が壊れている (= 開始前に verify が pass する / コマンドが壊れている) ことに、
  今までは runner Job が起動してから気づいていた。P-0012 と P-0013 は連続で
  `spec_error` で死に、拒否権窓の予告 2 回・Job 2 回・Discord 通知 2 通を捨てた。
  VISION「同じ失敗を 2 回したら仕組みの不備」。**proposed → announced の間で殺す。**

役割分担 (heart の規律):
  - `classify()` は純関数。判定はここだけに在り、`reconcile.decide()` から呼ばれる
  - `run_gate()` / `run_verify_in()` は I/O。`heart.execute()` が action として呼び、
    生の実測レコードを `p["adopt_gate"]["verify"]` に書き戻す。reconcile は
    その生レコードを `classify()` に通して verdict を導く (信念でなく実測から導く)

`ops/runner/runner.py` の `run_verify()` と**測定として等価**でなければ意味がない
(ゲートが通して runner が弾く、が最悪)。実行の形 (`bash -c` / cwd / capture) は
そちらに合わせてある。片方を変えるときはもう片方も見ること。共通化は今回やらない
(1 PR 1 論点、CHARTER §4)。runner 側の開始前 all-fail ゲートも**残す** — ここは
予告の前段であって、runner 側の最後の砦を置き換えるものではない (二重に守る)。

判定の実測的根拠 (2026-08-08, bash 5.3 / git 2.54.0):
  - `bash -n -c '<cmd>'` は構文エラーのときだけ rc=2。存在しないコマンドでは rc=0。
    **構文エラーの唯一の判定手段で、実行の前に打つ**
  - 実行時 rc=127 = コマンドが無い
  - 実行時 rc=2 を構文エラーと見なしてはいけない。`grep -q x /nonexistent` も 2 を
    返す。これは正当な「まだ出来ていない」
"""

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import gitutil

# 閾値は ops/rules.json でなくモジュール定数にする。rules.json は ruleset の
# 人間レビュー必須パスで、触ると auto-merge が止まる (reconcile.REVIEW_TIMEOUT_HOURS と同じ流儀)
VERIFY_TIMEOUT_SECONDS = 120  # verify 1 本あたり。runner 側は 600s だが、ここは
# ビートを止めないことを優先する。120s を超える verify は broken_command として
# 予告を止める (通してから runner で溢れるより、spec を直させる方が安い)
GATE_TIMEOUT_SECONDS = 300  # ゲート全体。ビート (既定 120s) を何十分も止めない
SYNTAX_CHECK_TIMEOUT_SECONDS = 30
OUTPUT_TAIL = 2000

ALL_FAIL = "all_fail"
SOME_PASS = "some_pass"
BROKEN_COMMAND = "broken_command"


def _broken_reason(rec):
    """壊れた測定なら理由文字列、正当な測定なら None。"""
    if rec.get("syntax_error"):
        return "syntax_error"
    if rec.get("timeout"):
        return "timeout"
    if rec.get("not_found"):
        return "not_found (rc=127)"
    if rec.get("error"):
        return f"error: {rec['error']}"
    return None


def classify(results):
    """verify の実測レコード列 -> 判定。純関数 (この中で I/O をしない)。

    判定順は **broken_command > some_pass > all_fail**。壊れたコマンドが 1 本でも
    あれば測定そのものが信用できないので、他が全部 fail でも予告しない。
    タイムアウトは独立の verdict にせず broken_command に畳む (理由に timeout を残す)。

    verify が空の spec は「受入基準が無い」= 採択の不良であり、all_fail ではない。
    完成を宣言できる者が居なくなるので broken_command に落とす。
    """
    results = list(results or [])
    passed = [r.get("cmd", "") for r in results if r.get("ok")]
    broken = [
        {"cmd": r.get("cmd", ""), "reason": reason}
        for r in results
        for reason in [_broken_reason(r)]
        if reason
    ]
    if not results:
        broken = [{"cmd": "(なし)", "reason": "no_verify_commands"}]
    if broken:
        verdict = BROKEN_COMMAND
    elif passed:
        verdict = SOME_PASS
    else:
        verdict = ALL_FAIL
    return {"verdict": verdict, "passed": passed, "broken": broken}


def describe(verdict_info):
    """classify() の結果を人間 (と Discord) 向けの 1 行にする。"""
    parts = []
    if verdict_info.get("passed"):
        parts.append("開始前に pass: " + "; ".join(verdict_info["passed"]))
    for b in verdict_info.get("broken", []):
        parts.append(f"壊れたコマンド: {b['cmd']} ({b['reason']})")
    return " / ".join(parts) or "(詳細なし)"


def _record(cmd):
    return {
        "cmd": cmd,
        "ok": False,
        "rc": None,
        "timeout": False,
        "syntax_error": False,
        "not_found": False,
        "output": "",
    }


def run_one(work_dir, cmd, timeout=VERIFY_TIMEOUT_SECONDS):
    """verify コマンド 1 本を work_dir で実行して実測レコードを返す。"""
    rec = _record(cmd)
    # 構文検査は実行の前に。実行時 rc=2 からは構文エラーを区別できない
    try:
        chk = subprocess.run(
            ["bash", "-n", "-c", cmd],
            cwd=work_dir, capture_output=True, text=True,
            timeout=SYNTAX_CHECK_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        rec["error"] = str(e)[:500]
        return rec
    if chk.returncode != 0:
        rec["syntax_error"] = True
        rec["rc"] = chk.returncode
        rec["output"] = (chk.stdout + chk.stderr)[-OUTPUT_TAIL:]
        return rec

    try:
        p = subprocess.run(
            ["bash", "-c", cmd],
            cwd=work_dir, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        rec["timeout"] = True
        rec["output"] = f"timeout after {timeout}s"
        return rec
    except OSError as e:
        rec["error"] = str(e)[:500]
        return rec
    rec["rc"] = p.returncode
    rec["ok"] = p.returncode == 0
    rec["not_found"] = p.returncode == 127
    rec["output"] = (p.stdout + p.stderr)[-OUTPUT_TAIL:]
    return rec


def run_verify_in(work_dir, verify, timeout=VERIFY_TIMEOUT_SECONDS,
                  overall_timeout=GATE_TIMEOUT_SECONDS, clock=None):
    """与えられたディレクトリで verify 列を 1 本ずつ実行する。

    clone から切り離してあるのは、単体テストがネットワークに出ずにここだけを
    突けるようにするため。全体の上限を超えたら残りは測らず timeout として畳む
    (測っていないものを「fail した」と記録しない)。
    """
    clock = clock or time.monotonic
    started = clock()
    results = []
    for cmd in verify:
        remaining = overall_timeout - (clock() - started)
        if remaining <= 0:
            rec = _record(cmd)
            rec["timeout"] = True
            rec["output"] = f"gate timeout after {overall_timeout}s (未実行)"
            results.append(rec)
            continue
        results.append(run_one(work_dir, cmd, timeout=min(timeout, remaining)))
    return results


def clone_fresh(repo_url, dest, branch="main"):
    """使い捨ての新品 clone を作る。

    **`--depth=1` を使わない。** shallow clone は `--single-branch` を含み、
    `remote.origin.fetch` が clone したブランチ 1 本だけになるため、以後
    `git fetch origin` を何度打っても `origin/ops-state` が生えない
    (`ops/memory/substrate.md`。P-0014 の worker が踏んだ)。ここでは full clone に
    加えて refspec を明示した fetch を打ち、`origin/main` と `origin/ops-state` の
    両方が見える状態を作る — verify に `git show origin/ops-state:projects.json` を
    含む spec (ダッシュボード系) がこれを要る。
    """
    gitutil.run(["clone", "--quiet", repo_url, str(dest)])
    gitutil.run(
        ["fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*"],
        cwd=dest,
    )
    gitutil.run(["checkout", "--quiet", "-B", branch, f"origin/{branch}"], cwd=dest)


def run_gate(repo_url, verify, branch="main", timeout=VERIFY_TIMEOUT_SECONDS,
             overall_timeout=GATE_TIMEOUT_SECONDS):
    """使い捨ての新品 clone で verify 列を実測してレコード列を返す。

    clone に失敗したときは例外を投げる (空の測定を返さない)。呼び出し側は
    `p["adopt_gate"]` を書かず、次のビートでやり直すこと — 測れなかったことを
    「all_fail だった」と取り違えると、このゲートの意味が消える。
    """
    # 固定パスを使わない。/tmp は Pod の生存期間を通じて持続し、前回の残骸を拾う
    # (ops/memory/substrate.md、run #132 の実事故)
    work = tempfile.mkdtemp(prefix="adoptgate-")
    try:
        clone_fresh(repo_url, Path(work) / "repo", branch=branch)
        return run_verify_in(
            Path(work) / "repo", verify, timeout=timeout,
            overall_timeout=overall_timeout,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

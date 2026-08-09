# P-0026 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

DoD (1)〜(6) を実装済み。**受入 5 項目とも自分で実測して green**
(2026-08-09、`project/p-0026` の作業ツリーでリポジトリルートから実行。rc は 5 項目とも 0)。
残りは wrapper の再実測とレビュー待ち。**このブランチは `ops/runner/` と `.github/` を
触るので ruleset の人間レビュー必須パスに当たり、auto-merge されない。**

## セッション記録

### s1 (2026-08-09) — 実装 3 コミット、受入 5/5 green

やったこと (PROJECT.md の設計方針をほぼそのまま実装した。設計との差分は下の「発見」に書いた):

1. `65a8784` runner 本体 (DoD 1〜4)
   - `Session.run()` の `stderr=subprocess.DEVNULL` → `subprocess.PIPE`。専用デーモン
     スレッドで読み切り、`collections.deque(maxlen=400)` に末尾行だけ保持。
     **stderr の到着では `last_event` を更新していない** (活動の定義は stdout のまま)。
   - 純関数 3 本をモジュール直下に追加: `classify_session_failure` /
     `parse_usage_limit_reset` / `mask_secrets(text, env)`。パターンは
     モジュール定数 `FAILURE_PATTERNS` (順序 = 判定順)。
   - `Session.run()` の戻りを `(outcome, usage, info)` に拡張、`run_session()` が
     `self.last_session = info` に置く (呼び出し側の署名は不変)。異常終了系の
     `write_result` に `failure_kind` / `stderr_tail` を載せた。
   - `mode_worker()` の while に usage_limit 枝。連続 error に数えず `time.sleep` で
     待って再開、待機予算 (`session_max_seconds`=7200) を超えるなら `waiting_quota` +
     `resume_after` を書いて **rc=0** で終える。
   - 上限で即死した回に 50,000 トークンの概算を付けない。
   - `ops/runner/tests/test_exit_reason.py` (+ `__init__.py`) — 18 tests。
2. `968f1e9` heart 側の最小分岐 (`ops/heart/reconcile.py` の `active` 枝) と
   `ops/heart/tests/test_reconcile.py` に `TestQuotaWait` (6 本)。全 101 tests green。
3. (このコミット) `ops/memory/substrate.md` に「claude セッション / 利用上限」節、
   `.github/workflows/ci.yml` の ops job に runner の unittest ステップ。

### 分かったこと / 実測した罠

- **`result` イベントの本文を無条件に分類へ混ぜてはいけない。** 成功した回の
  `result` フィールドは最終アシスタント本文であり、本文が上限の話題に触れているだけで
  `usage_limit` に誤分類する (まさにこのプロジェクトの worker が該当する)。
  `is_error` か `subtype != "success"` の回だけ拾うようにした。
- **偽 `claude` を PATH に置いた実機スモークを 2 本回した** (`mktemp -d` にスクリプトを置く)。
  (a) stderr に `ghp_…` + `Claude AI usage limit reached|1754697600` を吐いて rc=1 →
  `failure_kind=usage_limit` / `stderr_tail` は `***` にマスク済み / `reset_at` 復元 /
  tokens は 0 のまま (概算なし) を確認。(b) stderr 200KB を吐いて正常終了 →
  デッドロックせず `completed` / tokens 150。**stderr を読み捨てない実装の回帰検査に
  そのまま使えるので、次に触るときも同じ手が使える。**
- `parse_usage_limit_reset` の epoch `1754697600` は 2025-08-09 (2026 ではない)。
  テストの期待値もそれで固定してある。

### 設計 (PROJECT.md) からの意図的な差分 — レビューで見るならここ

- **`quota_wait_until` の再開判定で `running >= max_concurrent` を見ていない。**
  PROJECT.md は「既存の spawn 抑制と同じ扱い」で両方見ろと書いているが、
  `rules.runner.max_concurrent` は **1** で、待機中のプロジェクト自身が `active` として
  `running` に数えられている。見ると `running(1) >= 1` が常に真になり **永久に再開できない**。
  スロットは既に自分が占めているので再開しても同時実行数は増えない。breaker 抑制だけ残した。
- **待機中は job 梯子より先に `quota_wait_until` を評価している。** `waiting_quota` は
  rc=0 終了なので Job は succeeded で残る (active でも failed でもない)。梯子に落とすと
  TTL で Job が消えた時点で `job is None` → drift → 3 ビートで stalled になり、
  上限対策そのものがループを止める。テスト `test_waits_silently_until_the_deadline` が これを固定する。
- **breaker 中は `quota_wait_until` を消さずに保持する。** 消してから何もしないと次のビートで
  梯子に落ちて即 respawn してしまう。

### 発見 (スコープ外。curriculum が拾う想定)

- `FAILURE_PATTERNS` は**まだ実測されていない候補**。本 PR が merge されて初めて実際の
  文言が `stderr_tail` に残る。**merge 後、最初の異常終了回の result.json を見て
  表を実文字列で置き換える**フォローが要る (substrate.md にもその旨を書いた)。
- `auth` / `network` は記録するだけで制御を変えていない (spec の「やらないこと」通り)。
  認証失敗の即時打ち切りは、実 `stderr_tail` が集まってから別プロジェクトで。
- `ops/validate.py` が backlog について warning を 2 件出す (T-0035 の refs 切れ / todo 0 件)。
  P-0026 とは無関係の既存 warning で、rc は 0。

### 次のセッションへの一言

**実装はもう無い。** 受入 5/5 は自分で実測して green、heart 101 tests / runner 18 tests /
`ops/validate.py` / ops job の他スクリプト (version_sync, pvc_usage, health_reporter_target,
doc_commands, feedback, dashboard build) も全部 rc=0 を確認済み。
次のセッションが起きたなら、それは wrapper の再実測が落ちたかレビューの差し戻しがあったとき。
**まず `VERIFY_STATUS` と `REVIEW_FINDINGS` を読むこと**。何も指摘が無いのに起きた場合は、
上の「設計からの意図的な差分」3 点が争点になっている可能性が高いので、そこを説明する形で直す。

# P-0211 — PROGRESS

## 2026-08-23 セッション 1 — 実装一式 (受入 3 項目を green 化)

### やったこと

- `ops/check_health_freshness.py` 新設。位置引数に latest.json パス、`--max-age-hours N`
  (既定 `DEFAULT_MAX_AGE_HOURS = 6.0` — 書き手 CronJob 30 分毎に対し 12 サイクル分の余裕)、
  stale (fail-closed 含む) で rc=3 / fresh で rc=0 / 引数誤りで rc=2。
  雛形は `ops/check_heartbeat_fresh.py` (P-0027) どおり: stdlib のみ・import 時副作用なし・
  純粋関数 + main() へ I/O 集約・`--now` で試験可能・未来時刻は skew として fresh 側。
  cooldown / 重複抑止は持たない (仕様の rc 表に無い。→ 発見 2)。
- `ops/tests/test_health_freshness.py` 新設 (28 本)。fresh/stale 境界 (>= 側が stale)、
  fail-closed 4 形状 (欠落/破損/非 object/generated_at 異常)、CLI の rc 契約、
  投稿本文が triage.classify で review_needed に倒れること (stop/resume キーワード行頭禁止 +
  50 文字超え + veto/ack パターン禁止) を固定。
- `ops/tests/fixtures/health/stale-latest.json` 新設 (`generated_at = 2020-01-01T00:00:00Z`)。
  `--now` を渡さない実 verify でもいつ実行されても stale になるよう紀年より前に置いた
  (テストで 365 日以上古いことも担保)。
- `.github/workflows/watchdog.yml`: 既存 heartbeat job へのステップ追加のみ
  (PROJECT.md 設計どおり job の増築なし)。末尾に fetch step (FETCH_HEAD 経由・fetch 失敗でも
  止めず fail-closed に渡す) と check step (rc=3 で issue #56 コメント + Discord incident +
  job fail)。dry_run 入力は heartbeat ステップと同じ意味論で尊重。

### verify 実測 (すべて自分で回して green)

1. `python3 -m unittest ops.tests.test_health_freshness` → Ran 28 tests, OK
2. fixture + `--max-age-hours 3` → rc=3 ちょうど (PASS)
3. `grep -q check_health_freshness .github/workflows/watchdog.yml` → PASS

追加の実測: 全 ops suite (`discover -s ops/tests -t .`) 382 本 OK。
watchdog.yml から check step の run を取り出し bash -n 通過 + 実走 (dry_run で stale/fresh
両経路、exit と summary 生成を確認)。実物 `origin/ops-health-report:ops/health/latest.json`
(generated_at 2026-08-23T15:30:05Z) は既定閾値で fresh / rc=0。

### 分かったこと / 罠

- **watchdog.yml の block scalar 内で heredoc は case の内側に書けない。** YAML が共通
  インデントを剥がす関係で heredoc 終端子は列 0 必要だが、case 分岐の内側は base より深く
  なるため終端子が揃わない。`python3 -c '...'` にして、コード行を run ブロックの base
  インデント (10 桁) で書くことで解決した (剥がした後に Python ソースが列 0 に揃う)。
  同じ file 内の drill step は case の外なので heredoc のままで正しい。
- **`secrets.DISCORD_WEBHOOK_URL` は未検証の仮定。** この環境に gh が無く repo secrets を
  確認できない。Doppler 側の鍵名 (`apps/autopilot/external-secret.yaml`) に合わせた。
  未設定でも Discord leg を警告つきでスキップし、issue コメント + job fail は生きる
  (通知経路の単一点依存を避ける)。**人間レビュー時に secret の登録有無だけ確認してほしい。**
- dry_run=true で stale 検知時も exit 0 (job 成功) にしてある。既存 heartbeat ステップの
  drill 意味論と揃えた。訓練で job を赤くしない、が意図。

### 発見 (スコープ外。curriculum が拾うこと)

1. watchdog.yml の job 名 `heartbeat freshness` は、health-report 検査を含めた今の内容に対し
   短くなっている。Actions UI の見かけだけの話だが、次に .github/ を触る人が変えてよい。
2. 本チェックには再投稿抑止 (cooldown) がない。reporter が長時間死んでいると 30 分毎に
   issue コメント + Discord が飛ぶ (最大 48 件/日)。heartbeat 側の
   REPOST_COOLDOWN_SECONDS 方式 (issue comments を読んで自分のマーカーを探す) の流用で
   防げるが、comments 取り込みの追加 surface になるため本プロジェクトでは仕様どおり
   見送った。運用で騒音になったら別案で。

### 次のセッションへの一言

受入 3 項目とも wrapper 実測待ちのはず。落ちているなら真っ先に疑うのは (a) fixture の
generated_at が未来日になっていないか (2020 固定なので通常あり得ない)、(b) watchdog.yml を
誰かが書き換えて grep が消えたか。レビュー指摘が出たら triage 誤爆テスト (TestBuildBody)
と Discord webhook の扱いを中心に見ること。merge は CODEOWNERS の人間待ち (予告済み)。

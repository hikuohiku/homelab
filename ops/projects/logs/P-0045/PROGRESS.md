# P-0045 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### s1 (2026-08-10) — 配線を通し、初回の所見を実データで出した

**verify 3 項目とも green** (自分で実行して確認)。テストは heart 136 / ops 38 / runner 28 で全 OK。

やったこと (commit 4 本):

1. `reconcile.py` に `_critic_due` + `spawn_critic` / `consume_critic` / `notify_critic`。
   テスト 11 件 (`TestCritic`)
2. `metrics.summarize_beats` / `summarize_stalled` (純関数)。テスト 12 件
3. heart 側の実体: `facts.collect_critic` / `spawn.build_job(project_id=)` /
   `Heart.prepare_critic_input` / `Heart.critic_summary` / `notify.IMMEDIATE_TYPES` に `critic`
4. `critic.md` 全面書き直し + `curriculum-generate.md` に `/data/critic/` を追加 +
   `summarize_beats` に壁時計 3 分割

**DoD (4) は満たしたが、経路は spec の想定と違う。**
`/data/critic/2026-08-10.md` に所見 5 件を実データから出した (最重要:
「人間向けダッシュボードが未レビューのフィーチャーブランチから今日 6 回上書き公開された」)。
ただし **critic Job としては走らせていない** — 下記の罠を参照。

#### 分かったこと (次のセッションが同じ穴に落ちないために)

- **worker のシェルに `CLAUDE_CODE_OAUTH_TOKEN` は無い。** PROJECT.md 設計方針 6 の
  「worker Job の env にはあるので動くはず」は**誤り**。実測: `env` に存在せず、
  `claude -p` は `Not logged in · Please run /login` を返す (`CLAUDE_CODE_CHILD_SESSION=1`)。
  入れ子のセッションはこの器では起こせない。**同じ検証を再試行しないこと。**
  → PROJECT.md 設計方針 6 の後半 (自分で critic の役をやる) の経路で実施した
- **したがって未検証なのは「Job として起動したときの経路」だけ**: runner の
  `mode_oneshot("critic")` によるプロンプト読み込みと `result.json` 書き出し。
  heart 側 (入力生成 → Job spec 組み立て → 所見の読み出し → Discord 本文) は
  実 `/data` に対して実行して確認済み。**merge 後の初回 spawn がその最後の実測になる**
- `prepare_critic_input` は実 `metrics.jsonl` 2393 行に対して 1292 ビート分を集計できた。
  `downtime_seconds` は 0、ビート間隔は中央 65s / 最大 131s (600s 超の空きは 0 回)
- `build.py` の検分は `env -u AUTOPILOT_GITHUB_TOKEN` を**必ず**前置する。この罠は実在する:
  P-0044 の worker が今日これを落として `ops-dashboard` に 6 回 push している (所見 1 の証拠)
- 生成物 (`index.html` / `prs.json`) は commit に混ざっていない (`git status` で確認済み)

#### 未解決 / 次に効きそうなこと

- `spawn_critic` の action は heart の `execute()` に届くが、**shadow モードでは
  ログだけ**。本番 (`HEART_MODE=active`) で初めて Job が出る
- `_critic_due` は「活動が `last_critic_at` より**厳密に後**」を要求する。spawn と活動が
  同じビートに乗ると次は due にならない (テストで固定済み)。意図通りだが、
  「毎日必ず 1 回」ではなく「活動の翌ビート以降に 1 回」になることは覚えておく
- critic の所見 5 件は `/data/critic/` にあるだけで、まだ誰も拾っていない。
  curriculum が次に回るとき `curriculum-generate.md` 経由で読むはず (未実測)

## 発見 (スコープ外。curriculum が後で拾う)

- **`build.py` が引数なし実行で `ops-dashboard` へ publish する既定は危険。**
  検分のための実行と人間への公開が同じコマンドで、止める手段は散文の約束だけ。
  今日 6 回、未レビューのブランチの内容が人間の見る面を上書きした。
  `--push` / `DASHBOARD_PUBLISH=1` の明示要求にすべき (所見 1)
- **全 18 プロジェクトの `budget.used_tokens` が 0。** ダッシュボードの予算メーターは
  全件で嘘をついている。これを直す spec (P-0023 / P-0025) は 2 件とも `error` で停止し放置 (所見 2)
- **停止 (`stalled`) 4 件が誰にも再訪されない。** 終端として projects.json に残るだけで、
  理由 (`error` 2 / `spec_error` 2) を人間に差し戻す経路が無い (所見 2)
- **merging の滞留に目安が無い。** P-0026 は merging に 17h32m 座り、通知は 0 通
  (`MERGING_TIMEOUT_HOURS = 24` 未満)。滞留時間をダッシュボードの行に出すべき (所見 3)
- **同型 spec の二重採択を止める仕組みが無い。** P-0012/P-0013 (両方 `spec_error`)、
  P-0023/P-0025 (両方 `error`) が同一 title。防止は curriculum プロンプトの自己申告だけで、
  採択ゲートは同型性を見ない (所見 5)
- `consolidation` / `chore` は critic と同じ「Phase 3 で配線」の未接続モードのまま残っている

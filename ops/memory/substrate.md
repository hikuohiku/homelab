# substrate — in-cluster 実行環境の実測制約

autopilot namespace の Pod (heart / runner / reviewer / …) が動く環境の**実測された事実**。
旧 CHARTER §5.5 から移植 (2026-08-07)。推測を書かない。substrate が変わったら実測し直す。

## GitHub

- `api.github.com` に到達できる (旧クラウドサンドボックスの 403 は無い) — verified_at: 2026-08-05, CHARTER §5.5
- PR 作成 / merge (`merge_method: merge` 固定。squash/rebase は無効化済み) / issue コメント /
  check-runs 取得 / Contents API 書き込み、いずれも `AUTOPILOT_GITHUB_TOKEN` で実証済み — verified_at: 2026-08-05
- ブランチ削除 `DELETE /repos/.../git/refs/heads/<branch>` は 204 で成功する — verified_at: 2026-08-05, run #57
- `main` への直 push は ruleset が拒否する (例外なし。記録も PR 経由) — verified_at: 2026-08-04
- **`workflow` scope は 2026-08-07 に人間がトークンへ追加した。** それ以前は
  `.github/workflows/**` を含む push 自体が拒否された (T-0153/T-0156/T-0157 が
  needs-human 化した原因)。以後は CI 配線もエージェントの仕事 — verified_at: 2026-08-07 (人間の申告。初回 push で実測すること)
- ruleset (必須チェック一覧・パス保護) は API からも CLI からも変更できない。人間専有 — verified_at: 2026-08-04, issue #56
- 一覧系 API は `per_page=100` を明示する。省略すると一部しか返らない — verified_at: 2026-08-05
- PR の `merged` フィールドは信用しない (closed 全件で false を返す実測あり)。
  `merged_at` の非 null で判定する — verified_at: 2026-08-05, CHARTER §7.1

## git (clone / refspec)

> この節は consolidation ではなく P-0015 の worker が追記した (spec の DoD (4) が名指しで
> 要求している例外。README「書き手は consolidation の PR のみ」の唯一の破れ)。

- **`git clone --depth=1 <url>` は `--single-branch` を含む。** clone 直後の
  `remote.origin.fetch` が `+refs/heads/<clone したブランチ>:refs/remotes/origin/<同>` の
  **1 本だけ**になり、以後 `git fetch origin` を何度打ってもこの refspec しか使われない。
  `origin/main` も `origin/ops-state` も生えず、`git show origin/ops-state:projects.json` が
  rc=128 で**静かに**落ち続ける (「ファイルが無い」と区別が付かない) — verified_at: 2026-08-08, P-0014 の worker が実際に踏んだ / P-0015 で再実測
- 復旧は明示 refspec `git fetch origin '+refs/heads/*:refs/remotes/origin/*'`。
  打った直後に `origin/main` が生えるのを実測した。**shallow のままでも
  `git show origin/<branch>:<path>` は成功する**ので `--unshallow` は要らない — verified_at: 2026-08-08, P-0015
- 他ブランチを見る必要がある使い捨て clone では `--depth=1` を使わず、full clone +
  明示 refspec の fetch にする。実装は `ops/heart/adoptgate.py` の `clone_fresh()` — verified_at: 2026-08-08, P-0015

## Kubernetes

- 旧 `autopilot-reader` ClusterRole は get/list のみ。secrets と pods/log は読めない。
  heart 用 SA (`autopilot-heart`) は autopilot namespace 内の jobs create/delete と
  pods/log を追加で持つ (apps/autopilot/rbac.yaml) — verified_at: 2026-08-07 (manifest。適用後に実測すること)
- 書き込み系 SA (`autopilot-writer`) は capability 宣言 + 予告済みプロジェクトの Job にのみ注入される — 設計 (2026-08-07)
- node01 は単一ノード 4 vCPU / 11.7 GiB allocatable。requests 合計は約 1.2 CPU / 2.6 GiB — verified_at: 2026-08-06
- **memory limits は実測の裏付けなしに付けない** (OOMKill は回復しない。CPU limits は throttle なので別扱い) — verified_at: 2026-08-05, T-0055 事故
- `spec.timeZone` を明示しない CronJob の schedule は **JST** で評価される
  (node01 の `time.timeZone = "Asia/Tokyo"`)。アプリ内部タイマー (immich 内蔵 backup 等) は対象外 — verified_at: 2026-08-06, T-0125
- Job リソースを同名で再適用するものには最初から
  `argocd.argoproj.io/sync-options: Force=true,Replace=true` を付ける
  (`.spec.template` は immutable。2 回独立に踏んだ) — verified_at: 2026-08-06, T-0108/T-0111
- apps root Application は `prune: true`。render 結果から消えたオブジェクトはクラスタからも消える
  (PVC ならデータごと)。CI の manifest-diff が唯一の機械的歯止め — verified_at: 2026-08-05, T-0036

## コンテナ / ファイルシステム

- `/tmp` は Pod の生存期間を通じて持続する。固定パスの一時ファイルは前回の残骸を拾う
  (無関係な本文で PR #323 が作られた実事故)。`mktemp` を使う — verified_at: 2026-08-06, run #132
- claude CLI は未 trust のワークスペースで repo 側 `.claude/settings.json` の
  permissions.allow を無視する (18 件無視の実測)。`~/.claude.json` に
  `hasTrustDialogAccepted` を書いてから起動する (loop.sh / runner.py が実装) — verified_at: 2026-08-05
- イメージ (`ghcr.io/hikuohiku/homelab-autopilot`) に入っているもの: git, python3 (+py3-yaml),
  curl, bash, restic, kubectl v1.35, chromium + Noto CJK, node22 + claude-code (npm pin 無し)。
  無いもの: gh, terraform, kustomize 単体 (`kubectl kustomize` は使える), nix, just, sops — verified_at: 2026-08-06
- restic バイナリはあるが B2/restic の credential はエージェント環境に無い (allowlist で機械検査) — verified_at: 2026-08-07

## claude セッション / 利用上限

> この節は consolidation ではなく P-0026 の worker が追記した (spec の DoD (5) が名指しで
> 要求している例外。README「書き手は consolidation の PR のみ」の破れは P-0015 に次いで 2 例目)。

- **アカウントの利用上限は器の外側の事実であり、プロジェクトの停滞ではない。** 器 (runner /
  reviewer / curriculum の各セッション) は人間の対話セッションと**同一サブスクリプションを
  共有している**ので、人間が対話で使った分だけ器のセッションが即死する。器の実装や spec の
  難易度とは無関係に起きる — verified_at: 2026-08-09, 2026-08-08 の実績 (26 セッション / 名目 $50.9 を使って
  プロジェクト 0 件前進、P-0023 と P-0025 が両方 stalled)
- 上限で死んだセッションは **実消費ゼロ**。runner の「トークン不明なら 50,000」概算を
  適用すると、待って再開する前に soft cap が尽きる (`ops/runner/runner.py` の `Session.run()` で
  `failure_kind == "usage_limit"` の回だけ概算を外している) — 設計 (2026-08-09)
- 死因は `claude` の **stderr にしか出ない**。2026-08-09 以前の runner は
  `stderr=subprocess.DEVNULL` で起動していたため、器に残る記録は「3 回連続で異常終了」だけで、
  上限と本当の実装詰まりが同じ顔に見えていた (同じ案を作り直した P-0023→P-0025 の往復の原因)。
  現在は result.json の `failure_kind` (`usage_limit`/`auth`/`network`/`unknown`) と
  `stderr_tail` (マスク済み末尾 2000 文字) に残る — verified_at: 2026-08-09, P-0026
- **上限の実文字列はまだ観測できていない。** `ops/runner/runner.py` の `FAILURE_PATTERNS` は
  claude CLI の既知の出力形を根拠にした候補 (`Claude AI usage limit reached`、
  `…reached|<epoch>`、`rate_limit_error`、`429` + `rate limit` 等) であり、実測ではない。
  実際の文言を観測したらその回の `stderr_tail` を証拠に表とテストへ追記すること — 2026-08-09 時点で未実測
- 上限と判定した回は「3 回連続 error」に数えず、reset 時刻 (取れなければ 900 秒) まで待って
  同じセッションを再開する。待機予算は `rules.runner.session_max_seconds` (7200s) で、
  超える場合は stalled ではなく result state `waiting_quota` + `resume_after` で終える。
  heart は `active` のまま `quota_wait_until` まで待って runner を出し直す — 設計 (2026-08-09), P-0026
- **「上限は停滞ではない」は「無限に待ってよい」ではない。** runner の 7200s は 1 プロセス内の
  上限にすぎず、`waiting_quota` → respawn → また `waiting_quota` の**周回そのものには時限が無い**。
  `max_concurrent` は 1 なので、黙って待ち続ける 1 件が他の全プロジェクトのスロットを塞ぐ。
  連続待ちを `quota_wait_count` で数え、`reconcile.QUOTA_WAIT_MAX_ROUNDS` を超えたら
  `quota_wait_exhausted` で人間に渡す (上限が明けていないか、死因の判定が誤っている)。
  reconcile.py 冒頭の不変条件「恒久的に黙って待つ状態を作らない」に例外を作らない —
  設計 (2026-08-09), P-0026 レビュー指摘 [1]
- 上限は **initializer / worker ループ / reviewer の 3 箇所**で制御に効く。特に
  **新規プロジェクトの最初のセッション (initializer) こそ最も上限に当たりやすい**。
  reviewer が上限で死んだ回は review.json を書かない — 書くと `verdict=fail` として
  `review_cycles` を消費し、`max_cycles` で `review_rejected` = ここでもループが止まる
  (heart 側の `REVIEW_TIMEOUT_HOURS` × `REVIEW_MAX_RETRIES` の再試行に任せる) —
  設計 (2026-08-09), P-0026 レビュー指摘 [2][3]

### opencode CLI の死因出力 (2026-08-22 移行当日の実測)

> この項も P-0101 の worker が追記した (spec の DoD が名指しで要求する例外。README
> 「書き手は consolidation の PR のみ」の破れは P-0015 / P-0026 に次ぐ 4 例目)。
> 実測原本は `ops/tests/fixtures/engine_stderr/`、分類への配線テストは
> `ops/tests/test_failure_patterns.py`。opencode CLI v1.18.21、model
> opencode-go/ox-alpha-free。

- 死因は **stderr に出ない**。成功・失敗とも stderr は常に空 (0 バイト実測) で、
  失敗時は stdout に `type=error` の JSON イベントが流れる。「P-0026 時代の
  stderr_tail」の実体は `consume_stream_event()` が拾う `error.data.message` に変わった
- 鍵が**誤っている**: `APIError` / `Invalid API key.` / statusCode 401 → auth に分類可能
- 鍵が**無い** (env 未設定 = spawn.py の secret 消滅に相当): `UnknownError` /
  `Unexpected server error. Check server logs for details.` → **auth に分類できない**
- ネットワーク断 (接続拒否も DNS 失敗も同一文言):
  `APIError` / `Cannot connect to API: Unable to connect. Is the computer able to access
  the url?` → network に分類可能 (`cannot connect to api` を P-0101 で追加)
- HTTP 429 (レート制限): **`UnknownError` に潰され上限情報は完全消失** → usage_limit に
  分類できない。openai-compatible / anthropic 両 SDK 経路・両レスポンス形式 (OpenAI 形 /
  Anthropic 形) で再現した。鍵未設定と同一出力になるため、unknown 以外への分類は不可能
- **opencode の本物の上限メッセージはまだ観測できていない** (ローカルモックによる CLI
  出力形の実測まで。zen API が実際に何を返すかは未観測)。上限で死んだ回の result.json
  `stderr_tail` を証拠に表とテストへ追記すること。**2026-08-22 時点では「上限死が
  unknown に落ちる」経路が実在し**、3 連続 error 判定から stalled 化する条件が残る
  (26 セッション空費の再演条件。reset 時刻抽出 `parse_usage_limit_reset()` も claude 形専用のまま)

## 観測経路 (壊すと自分の異常を誰も検知できなくなる)

- heartbeat 行 `[autopilot] <ts> iteration #N start|end exit=<rc> elapsed=<n>s` の産出元は
  `ops/heart/heart.py` の `log()` (旧 loop.sh から引き継いだ書式)。
  `apps/ops-health-reporter/report.py` の HEARTBEAT_RE と結合しており、変えるときは同時に変える。
  この結合と観測対象 (Deployment `autopilot-heart` / label `app=autopilot-heart`) は
  `ops/check_health_reporter_target.py` が CI (ops job) で機械検査する — 注意書きではなく
  片側だけ変えれば落ちる。**replicas >= 1 の Deployment を正とする**規則付き
  (退役済み `autopilot` を指したままだった drift が P-0011 の発端) — verified_at: 2026-08-08
- report.py のログ取得は `sinceSeconds=7200`。ビート周期 (`HEART_BEAT_SECONDS`, 既定 120s) を
  大きく変えるならここも見直す — verified_at: 2026-08-08
- 健全性レポートは ConfigMap `autopilot/ops-health-report` の `latest.json` キー。
  最新 1 点のみ (上書き) で履歴は持たない。GitHub の同名ブランチ経由は Phase 5 で廃止
  (書き手も読み手もクラスタ内) — verified_at: 2026-08-25
- coder / immich / vaultwarden の ArgoCD `Degraded` について (2026-08-22 に P-0111 が訂正):
  旧注記「T-0106 由来・鍵登録で自然解消する」は半分だけ正しかった。ExternalSecret は
  2026-08-07 の作成時から一度も SecretSyncedError になっておらず、鍵は最初から通っていた。
  実際の源泉は **backup CronJob の子 Job 失敗** (ArgoCD v3.2.1 `resourceHealthSource: appTree`
  が live の Job 失敗を Application health に反映) で、health 履歴では 2026-08-10 夜〜翌
  08-11 夕方 (17:45Z の成功 run で解消) と 08-22 夜にだけ Degraded になり、08-12〜08-21 は
  終日 Healthy だった (=「16 日間 Degraded」という認識は誤観測)。Job 失敗の奥の一次原因は **Backblaze B2 の download cap 超過**
  (`download_cap_exceeded`。アカウント単位で鍵の種類に無関係。usage counter は毎日
  00:00 UTC リセット — 公式ドキュメント + p0111-cap-watch による実測どおり 08-23T00:04Z に回復、
  手動 backup 成功で全アプリ Healthy へ復帰)。一次原因の実名と修繕手順は
  ops/projects/logs/P-0111/root_cause.md。
  「既知事象だから」と latest.json の断片で決めつけず、直近 backup Job の成否を見ること
  — verified_at: 2026-08-23

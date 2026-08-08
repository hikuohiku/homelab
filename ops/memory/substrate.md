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
- ops-health-report ブランチの `ops/health/latest.json` は最新 1 点のみ (上書き)。
  傾向は `ops/health/history/YYYY-MM-DD.jsonl` — verified_at: 2026-08-05
- coder / immich / vaultwarden の ArgoCD `Degraded` は T-0106 (append-only 鍵の Doppler 未登録)
  由来の既知事象。新規異常ではない。鍵が登録されれば自然解消する — verified_at: 2026-08-06

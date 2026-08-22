# P-0107 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと / 次への一言 を書く -->

### initializer (2026-08-22)

PROJECT.md を起票した。受入 2 項目は全て failing を実測済み (詳細は PROJECT.md)。
実装には着手していない。

### worker #1 (2026-08-22) — 決定論パススルー本体を実装 (受入 2 項目を green 化)

**やったこと**:

- **機構選定を実物で確定**: digest pin 済み image
  (ghcr.io/openclaw/openclaw@sha256:6a31d44b…, 2026.7.1) を registry API から層を引いて
  ソースを実読した。結果、spec (b) の前提「ingress-spool-* / ログを tail」は実態が違った:
  - spool の実体は **SQLite 状態 DB** `/home/node/.openclaw/state/openclaw.sqlite` の
    `channel_ingress_events` テーブル (`dist/ingress-queue-Cxwis8TN.js`)。「ingress-spool-<account>」
    というパスはキュー名導出に使われるだけで、ファイルスプールは存在しない
  - `payload_json = {version:1, updateId, receivedAt(epoch ms), update:<Telegram 生 update>}`。
    queue_name は `JSON.stringify([pluginId, accountId])` = `'["telegram","default"]'`
  - 受信 update は **allowlist フィルタより先に全件 spool される** (polling-session.ts)。
    処理済みも墓石 (status='completed') として 30 日 / 上限 1000 件残る → 全 status 走査で
    1 update = 1 回だけ拾える。取りこぼし無し
  - `/tmp/openclaw/openclaw.log` は pino 系ローテートログで、メッセージ本文の抽出元としては
    構造化 DB に劣る → 使わない
- **apps/openclaw/bridge.py を新設** (python:3.14-alpine sidecar `feedback-bridge` で常駐):
  DB を読み取り専用 (`mode=ro`) で走査し、allowlist 内送信者 (TELEGRAM_ALLOWED_USER_ID 一致)
  の message.text/caption を生のまま (trim 無し) note 形式で ops-feedback へ PUT。
  LLM を経由しない強制が「DB 読み取り」というコードレベルの事実になる。OpenClaw 内部には触れない
- **ops/tests/test_openclaw_bridge.py 新設** (29 tests): 変換・allowlist fail-closed・
  id/received 形式 (dashboard route.ts 同型)・kind を付けないこと・cursor 挙動を固定
- deployment.yaml に sidecar 追加 + config-revision "8" へ。external-secret.yaml に
  AUTOPILOT_GITHUB_TOKEN 追加。kustomization.yaml に configMapGenerator
  (ops-health-reporter 同型)。inventory.json に openclaw-bridge-image 追加

**verify 実測 (green)**: grep #1 ok / unittest 29 件 OK。CI 相当も自力実測:
validate.py (0 error), check_credential_map.py (ok — AUTOPILOT_GITHUB_TOKEN /
openclaw-credentials は既存宣言のため地図更新不要だった),
check_version_sync / pvc_usage_script_sync / health_reporter_target / doc_commands /
feedback 全 ok, `kubectl kustomize apps/openclaw` と apps root rc=0, yaml parse all ok,
ops/tests discover 183 件 OK。ruff F821 はこの環境に ruff/pip 無くて未実行
(bridge.py は import + 全関数がテストで走るため module-level NameError は検出済み)。

**分かったこと / 次への引き継ぎ**:

- **実メッセージ 1 通での実測は未完了** (DoD の本体)。TELEGRAM_BOT_TOKEN /
  TELEGRAM_ALLOWED_USER_ID / OPENCLAW_GATEWAY_TOKEN が Doppler 未登録のため Pod は
  CreateContainerConfigError 待機 (想定どおり)。鍵登録後に送信テストすること。
  観察ポイント: bridge コンテナログに `saved ops-feedback:...` 行 / ops-feedback ブランチに
  `<id>.json` / gateway 側の応答成否と無関係に保存されること (gateway を止めても spool から
  保存されるのが決定論パススルーの証明になる)
- **初回起動は履歴を既読化するだけ** (heart collect_feedback のレビュー指摘 [7] 同型)。
  cursor ファイル `/home/node/.openclaw/bridge-cursor.json` (PVC, update_id 透かし) を消すと
  再初期化される。逆に PVC 復旧で update_id が巻き戻ると二重保存しない (skip 側に倒した設計)
- sidecar は TELEGRAM_ALLOWED_USER_ID secretKeyRef も参照するため、鍵未登録なら gateway と
  共倒れで起動しない (CreateContainerConfigError)。bridge 単体の待機状態は存在しない
- sqlite 読み取りは WAL の reader なので gateway の書き込みと競合しない設計のはず
  (mode=ro, timeout=5s, OperationalError は次 tick 待機)。実測は上記の送信テストで確認する

### worker #2 (2026-08-22) — review 指摘 2 件のうち rebase/annotation を解消。実メッセージ実測はこのサンドボックスからは不可 (詳細と実施手順を記録)

**やったこと**:

- **review 指摘 #2 (config-revision) を解消**: origin/main (d6d517b6, #503 先端) に rebase。
  deployment.yaml の annotation 部で予想どおりコンフリクト (#501 afc91404 が既に 7→8 済み)。
  解消して **"8"→"9" へ**、コメントも「#501 が 7→8 済みのため main 相対で確実に 1 rollout
  になるよう」と実態どおりに書き直した。rebase 後 head = `af1c7ca3` (force-with-lease で
  project/p-0107 更新。PR #504 も同 commit を追跡し ci.yml green を API 実測)
- rebase 後に verify を再実測し全 green: grep #1 ok / unittest 29 件 OK /
  validate.py 0 error / ops/tests discover 全 OK / `kubectl kustomize apps/openclaw`
  rc=0 かつ render 結果に revision=9, containers=[gateway, feedback-bridge],
  volume→ConfigMap 参照解決を確認。digest pin・replicas・Recreate・probe は無傷

**できなかったこと (重要 — 次のセッションはここから)**:

- **review 指摘 #1 (実メッセージ 1 通の実測) は本セッションでは実施不能だった。**
  前提「今すぐ実施できる」は **cluster アクセスを持つセッションでのみ真**。この worker
  サンドボックスには cluster 資格が一切無いことを実測した:
  - KUBECONFIG 未設定 / ~/.kube 無し / SA token 未マウント
    (/var/run/secrets/kubernetes.io/serviceaccount 不在) → `kubectl` は localhost:8080 refused
  - tailscale / direnv / doppler バイナリも無し (.envrc も無い) → Doppler 経由の資格取得も不可
  - ただし network は cluster 内 (pod IP 10.42.0.183/24)。API server 10.43.0.1:443 には届くが
    **401 (認証情報無し)**。argocd.tailae6c2.ts.net は名前解決できるが到達不可 (HTTP 000)
  - GitHub Actions はすべて ubuntu-latest で self-hosted runner 無し → CI 経由の deploy も不可
  - AUTOPILOT_GITHUB_TOKEN は env にあり git push / GitHub API は生きている (上記の push 実測)
  - Telegram 側も Bot API には受信 update を注入する手段が無い (bot 自身の送信は getUpdates
    に現れない)。実メッセージは allowlist 内の人間の送信以外に発生経路がない
- したがって worker #1 の記録「CreateContainerConfigError 待機」も実は観測ではなく推定だった
  可能性が高い (このサンドボックスからは pod 状態を見られない)。reviewer の実測
  (main の openclaw pod Running/Ready, secret 解決済み) が正。

**次への引き継ぎ (cluster アクセスのあるセッション / 人間がそのまま実行できる形)**:

1. `just preview openclaw project/p-0107` (head = af1c7ca3 以降。annotation 9 で 1 rollout)
2. pod Ready を待つ: `kubectl -n autopilot get pods -l app=openclaw -w`
   (2 コンテナ [gateway feedback-bridge] になっていること)
3. allowlist 内ユーザーから実メッセージ 1 通 (何でもよい) を送信
4. 証跡取得:
   - `kubectl -n autopilot logs <pod> -c feedback-bridge | grep saved`
     → `[openclaw-feedback-bridge] … saved ops-feedback:ops/feedback/inbox/<id>.json (update N, X chars)`
   - `git fetch origin ops-feedback && git ls-remote origin ops-feedback`
     → 新 commit。`ops/feedback/inbox/<id>.json` が `{id, source:"telegram", received, body}`
     形式 (kind 無し) であることを確認
5. 証跡 (id・commit hash・時刻) を PROGRESS.md に追記してから `just preview-reset openclaw`
- bridge は 15s poll。保存ログが出ないときは `kubectl -n autopilot logs <pod> -c feedback-bridge`
  全体を見る (初回起動行「cursor 初期化」→ その後新着のみ)。gateway 応答の成否と無関係に
  spool から保存されるのが決定論パススルーの証明 (merge 後観察でも同じでよい)

### worker #3 (2026-08-22) — review 指摘の技術的本体 (統合経路が一度も走っていない) に統合テストで対処。実メッセージ実測 (DoD 本体) は引き続き cluster 待ち

**やったこと**:

- **統合テスト 7 件を `ops/tests/test_openclaw_bridge.py` に追加** (`EndToEndRunOnceTest`)。
  review 指摘の「sqlite 読み取り → JSON 変換 → Contents API PUT の統合経路が unit テスト
  以外で一度も走っていない」に対し、**本物の `run_once()` を実物 SQLite × 実 HTTP で通した**:
  - 実 SQLite: journal_mode=WAL で spool DB を作り、gateway 相当の writer 接続を
    **開きっぱなし** (checkpoint 未実施 = -wal に未反映分が残る状態) で維持。
    bridge 側は mode=ro で読む — 本番 pod 内の競合条件を再現
  - 実 HTTP: localhost の ThreadingHTTPServer で git ref 取得/作成 + contents PUT を模し、
    urllib 経由の実リクエストを食わせる (モック関数ではない)
  - 固定した振る舞い: 初回 tick は履歴を既読化して保存しない / 新着が inbox 形式
    ({id, source:"telegram", received, body}, kind 無し, body 加工無し) で 1 ファイル保存 /
    再実行は二重保存しない / allowlist 外は GitHub にリクエストすら飛ばない /
    PUT 失敗時は cursor 不動 + HEAD_ATTEMPTS 加算 + 次 tick 再試行 /
    422 衝突時に id 振り直しで成功 / state DB 無しは例外で死なず待機
  - テスト内で pod ログと同じ `saved ops-feedback:...` 行も stdout アサート
    (`logs -c feedback-bridge | grep saved` で見るものと同型)
- **WAL 読み取りの前提を実測で確認**: writer 接続が開いたまま WAL に commit された行は
  mode=ro の reader から読める (Python 3.14 sqlite3 / このサンドボックスで実測)。
  「sqlite 読み取りは gateway の書き込みと競合しない設計のはず」(worker #1 記録) の
  「はず」が取れた。ただし本番 PVC 上での確認は merge 後観察に譲る
- verify 再実測すべて green: grep #1 rc=0 / unittest 36 件 OK (既存 29 + 追加 7) /
  ops/tests discover 143 件 rc=0 / validate.py rc=0 / `kubectl kustomize apps/openclaw`
  rc=0 (revision "9", bridge-script ConfigMap 解決)。deployment.yaml は無傷

**できなかったこと (変わらず — 次のセッションはここから)**:

- **DoD 本体の「実メッセージ 1 通で ops-feedback にファイルが現れることの実測」は未実施。**
  この worker サンドボックスも worker #2 と同型で cluster 資格が無いことを再実測した
  (KUBECONFIG 未設定 / ~/.kube 無し / SA token 未マウント → API server 10.43.0.1:443 は
  401 / tailscale・direnv・doppler・just バイナリ無し)。worker #2 の残した手順
  (preview → pod Ready [2 コンテナ] → allowlist 内ユーザーが送信 → saved ログ +
  ops-feedback ブランチの <id>.json を証跡取得 → PROGRESS 追記 → preview-reset) を
  cluster アクセスのあるセッションがそのまま実行すること。統合テストはこの DoD の
  **代替ではない** (Telegram 本物・本物 GitHub・本物 cluster が絡む最後の 1 マイル)

**分かったこと / 次への引き継ぎ**:

- `/tmp/opencode` は autopilot uid から書けない (root 所有 755)。一時ディレクトリは
  `mktemp` (worker prompt の指示どおり) か Python 内では `tempfile.mkdtemp()` 既定 (/tmp 直下)
- unittest のサマリ (Ran N tests / OK) は stderr、print デバッグやテスト内出力は stdout なので
  `2>&1 | tail` すると順序が崩れてサマリが見えなくなる。`2>file >/dev/null` で分離するのが確実
- 統合テストは 1 ケース約 0.6s (HTTP サーバ起動込み) で 7 件 +α。discover 全体は 6s 弱。
  CI への負荷は無視できる規模

## 発見 (curriculum へ)

- **ops/runner/tests/test_quota_flow.py の flaky テスト**:
  `test_three_usage_limits_are_not_three_consecutive_errors` が同じ commit で OK/FAILED が
  揺れる (backoff の sleep 記録に 0.001, 0.002... が混入する。時間依存と思われる)。
  本プロジェクトの変更前から再現する (stash して base commit でも同率で再現実測)。
  P-0101 領域 (runner) の修正候補


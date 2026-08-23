# P-0126 — 上流の新バージョンを誰も見ていない日が 17 日以上続いている — inventory 全対象の registry/release を毎晩機械で見にいく watcher を置き、#49 型の静放置を二度と成立させない

## 目的

`ops/inventory.json` の 42 target のうち上流の新版を見に行く経路はゼロで (全件 `latest` 欄空欄、
実測)、上流追従は 2026-08-06 以降誰もやっていない。vaultwarden 放置で同期が全停止した前科 (#49)
の構造原因は「人間も器も上流を見ていない」ことで、地層は今も続いている。既存の
`ops/check_version_sync.py` は manifest↔inventory の**内向き**整合しか見ず、上流→inventory の
**外向き**計器が存在しない。毎晩機械で見にいく watcher を置いて、静放置を二度と成立させない。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0126` の checkout で、リポジトリルートから実行)。

- [ ] `python3 -m unittest ops.tests.test_version_watch`
  — `version_watch.py` が存在し、ネットワークなし (レスポンス JSON を fixture 注入) の unit
  test が通ること。実測 rc=1 (ModuleNotFoundError: `ops.tests.test_version_watch` 未存在)。
- [ ] `test -f apps/version-watcher/cronjob.yaml && kubectl kustomize apps/version-watcher >/dev/null`
  — 毎晩走る CronJob の manifest が実在し kustomize render が通ること。実測 rc=1
  (`apps/version-watcher/` ごと未存在)。
- [ ] `grep -q 'version-watcher' apps/kustomization.yaml`
  — 新アプリが App of Apps root に資源登録されていること。root Application は `prune: true`
  (substrate.md) なので未登録のままクラスタに載せても消える。実測 rc=1。

**verify は DoD の下限であって DoD そのものではない。** spec dod (3) の「inventory 登録」
(新 CronJob 自身の image pin を `ops/inventory.json` に足す、後述) と dod (4) の「初回実行の
drift 実測件数を logs に残す」は verify が見張っていない — PROGRESS.md に証跡を残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- `ops/inventory.json` は `{"version", "_comment", "targets"}`。targets 42 件、kind 内訳は
  image 23 / github-action 7 / helm 5 / binary 3 / npm 2 / flake 1 / terraform 1。`upstream`
  の scheme 接頭辞は **`github:` 31 / `dockerhub:` 9 / `npm:` 2 の 3 種のみ** (実測)。target は
  `id/kind/name/current/file/match/upstream/policy` を必ず持ち、helm には `release_prefix`、
  重複 pin には `mirrors` が付くことがある。比較対象の値は各 target の `current`。
- 標準ライブラリのみの方針は repo 慣習 (`ops/rules.json` 冒頭コメント、CI の validate)。
  HTTP は `urllib.request`。「tools + unittest + fixtures」という既存パターンの見本が
  `ops/tools/sops_dependency_map.py` + `ops/tests/test_sops_dependency_map.py` +
  `ops/tests/fixtures/` にあり、これと同じ粒度にする (P-0027 の PROJECT.md も参照)。
- 結果の書き先パターンは `apps/ops-health-reporter/report.py` が丸ごと見本: GitHub Contents API
  (`ensure_branch` / `put_file`) で `ops-health-report` ブランチの `ops/health/latest.json` を
  上書きし、履歴は `ops/health/history/YYYY-MM-DD.jsonl` に追記。credential は ExternalSecret
  (ClusterSecretStore `doppler`) 経由で `GITHUB_HEALTH_REPORTER_TOKEN` を注入。Pod は
  `api.github.com` に到達できる (substrate.md GitHub 節、実測済み)。
- **同一ファイルへの複数 writer に注意**: ops-health-reporter が 30 分ごとに `latest.json` を
  全体上書きする。watcher が `version_drift` キーを載せるには GET→merge→PUT になり、Contents API
  の PUT は SHA ベースの楽観排他なので、GET と PUT の間に health-reporter が触れると失敗しうる。
  夜間 1 回対 30 分周期で衝突確率は低いが、SHA 不一致時に再取得してリトライする実装で潰す。
- CronJob の慣習: `spec.timeZone` 未指定の schedule は JST 評価 (node01 tz、substrate.md)、
  `concurrencyPolicy: Forbid`、`readOnlyRootFilesystem` + emptyDir `/tmp`、スクリプトは
  configMapGenerator (`disableNameSuffixHash: true`) で mount、memory limit を付ける場合は
  実測か裏付けのコメントを添える (T-0055 教訓)。「差分ありの日は briefing に畳む」は
  latest.json を読む autopilot 側の既存経路の仕事で、watcher は記録まで。

### 決めてあること

- `ops/tools/version_watch.py` は inventory を読み、upstream scheme ごとに registry /
  GitHub Releases API を叩いて最新版を取り、`current` と比較して drift 一覧を JSON で返す
  単一モジュールにする。HTTP 層は注入可能にして unit test は fixture JSON だけで通す
  (ネットワークなし、CI でもクラスタ外でも動く)。
- CronJob は `apps/version-watcher/` に namespace / rbac / external-secret / cronjob /
  kustomization の ops-health-reporter と同型の構成で新設し、`apps/kustomization.yaml` に
  resources 追加。自身の image pin も `ops/inventory.json` にエントリを足す (DoD (3) の
  「inventory 登録」。`ops-health-reporter-image` / `pvc-usage-reporter-image` の前例どおり)。
- 出力は `latest.json` の `version_drift` キー (対象 id / current / latest / upstream の列。
  drift 0 件の日は空配列)。history jsonl への追記も health-reporter に倣う。
- 初回実行は drift 件数を実測し、結果を本ディレクトリの logs に残す (dod (4))。

## やらないこと

- **drift 検出後の更新 PR 作成・適用**。spec 明記の通り次のプロジェクトに譲る。watcher は
  観測と記録まで (1 PR 1 論点)
- **inventory.json targets の `latest` 欄への書き込み・`check_version_sync.py` の改修**。
  内向き計器には触れない。watcher は読み取り専用で比較するだけ
- **Discord briefing への通知配線**。latest.json への記録まで。畳みは autopilot 側の仕事
- **policy=manual/pinned 対象の自動更新**。watcher は policy を問わず観測のみ
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot が直接 push する
  領域でコンフリクトする (CLAUDE.md)

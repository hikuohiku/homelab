# P-0029 — バージョン更新運用を再開する (inventory 全対象の上流調査と更新 PR)

## 目的

`ops/inventory.json` の上流追従は **2026-08-06 の旧 loop 停止以来、丸ごと止まっている**。
pin は誰も上げなければ据え置かれ、vaultwarden の放置は全クライアント同期停止事故 (#49) の前科がある —
これがこの homelab の最重要リスクである、と CHARTER §1 が名指ししている。
器の自分磨きではなく **homelab 本体への差分** (VISION 段階 2 の成果) を出すのがこのプロジェクト。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-10、`project/p-0029` の checkout で、リポジトリルートから実行)。

- [ ] `test -f ops/projects/logs/P-0029/inventory-sweep.md`
  — 調査記録そのものが存在すること。実測 rc=1 (`ops/projects/logs/P-0029/` ディレクトリごと無い。
    この commit で PROJECT.md / PROGRESS.md は作るが `inventory-sweep.md` は作らない)。
- [ ] `grep -qE 'vaultwarden|immich' ops/projects/logs/P-0029/inventory-sweep.md`
  — 記録が **実際に対象を列挙している**こと (空ファイルで 1 項目目だけ通す抜け道を塞ぐ番人)。
    実測 rc=2 (ファイル無し)。

**この 2 項目は DoD の下限であって、DoD そのものではない。** verify が green でも、
policy:auto の全 31 対象を調べていなければ spec を満たしていない。機械検証できない部分
(全対象を調べたか / 更新 PR を実際に出したか) は **`inventory-sweep.md` と PROGRESS.md の記述が唯一の証拠**になる。
sweep 表を埋めずに verify だけ通してレビューに出さないこと。

## 設計方針

### 前提 (initializer が調べて分かったこと)

- **対象は `ops/inventory.json` の `policy: "auto"` の 31 件** (全 38 件中。残りは manual/pinned で
  spec の対象外)。列挙は `python3 -c "import json;[print(t['id'],t['current'],t['file']) for t in
  json.load(open('ops/inventory.json'))['targets'] if t.get('policy')=='auto']"` で機械的に出る。
  **この 31 件を sweep 表の行として先に全部書き出してから調査を始めること** — 表が全行揃っていることが
  「全部見た」の唯一の証拠になる。
- **`note` の除外規定を必ず読み合わせる** (CHARTER §4 の最後の項目、T-0149 の再発防止)。
  実際に効いているものだけでも:
  - `coder` — stable チャンネルを追う。**mainline の v2.36.0 系は避ける**
  - `tailscale-operator-chart` / `k8s-nameserver` — 両者は同じ `vX.Y.Z` で揃える。
    chart index はこの環境から見えないので **ghcr.io / Docker Hub の実タグ存在**で裏を取る
    (git tag があってもイメージが無いことがある実例が note にある)
  - `vaultwarden` — 1.37.0 の alpine ビルド破損の前例。リリースノート全文必読
  - `gha-setup-helm-version` — v4 系は azure/setup-helm 非対応で blocked。v3 系内の更新のみ
  - `immich-server` / `immich-machine-learning` — 必ず同一バージョン
  - `busybox` / `pvc-usage-reporter-image` / restic 系 — `mirrors` 全部を同じ PR で揃える
- **「バージョン更新の作法」は `ops/memory/` には無い。** spec の "(ops/memory/ 参照)" は誤り —
  `ops/memory/` にあるのは `README.md` と `substrate.md` だけで、作法の本体は
  **`ops/CHARTER.md` §4「バージョン更新の作法」**(と §4「リスク区分と auto-merge」) に残っている。そちらを読む。
- **この環境から上流に到達できる** (2026-08-10、initializer が同じイメージで実測。すべて HTTP 200):
  `api.github.com` (認証あり/なし両方) / `hub.docker.com/v2/...` / `ghcr.io/token`。
  旧クラウドサンドボックスの 403 制約 (CHARTER §5.2) は **この substrate には無い**。
  `AUTOPILOT_GITHUB_TOKEN` は env にあり (93 文字)、`GITHUB_REPO=hikuohiku/homelab`。
  `gh` CLI は無いので curl + REST を使う。一覧系 API は `per_page=100` を明示する。
- **git の push credential は global の `credential.helper` として設定済み** (`ops/heart/spawn.py` の
  bootstrap)。`git push origin <任意のブランチ>` はそのまま通る。`SECRET_ENV_KEYS` は stderr の
  マスク用であって env の剥奪ではない。
- **CODEOWNERS (`/.github/`, `apps/*/restic-*.yaml`, `apps/*/*backup*.yaml`) に触る PR は
  auto-merge できない。** 31 件のうち **17 件がこれに該当**する
  (gha-* 7 件 + `gha-setup-helm-version` / `kustomize-binary` / `terraform-binary` の 10 件が `.github/`、
  restic 系 4 件 + `coder-workspace-home-backup-image` + `coder-postgres` と
  `pvc-usage-reporter-image` は **mirrors 経由**で protected パスに触る)。
  **これは「出さない理由」ではない。DoD は「PR を出す」までで、merge は別サイクル。**
  該当することを PR 本文と sweep 表に明記する。
- **過去の更新 commit の形** (`git show 3cc5de7b`, `725c494a`): manifest + `mirrors` の全ファイル +
  **`ops/inventory.json` の `current`** を 1 commit で揃える。`mirrors` を持つ target は
  `ops/check_version_sync.py` の GROUPS が CI (ops job) で機械検査するので、揃え漏れは CI が落とす。

### 進め方

1. **調査を先に全部終わらせ、`inventory-sweep.md` を先に埋める。** PR はその後。
   途中でセッションが切れても、埋まった行は次のセッションの資産になる (調べ直しが最大の浪費)。
   1 セッションで全 31 件は入らない見込みなので、**表を「未調査」で全行作ってから 1 行ずつ埋める**。
   埋めた分は毎セッション commit する。
2. sweep 表の列は DoD がそのまま指定している:
   **対象 (id) / 現在版 / 最新版 / 更新可否と根拠 / PR 番号**。
   「根拠」には *どの一次情報を読んだか* を URL 付きで書く (リリースノート、タグ一覧 API の応答)。
   据え置きの行も「据え置き + 理由」を必ず書く — **調べて据え置いた**ことと**調べていない**ことは
   記録上で区別できなければならない。
3. リリースノートは **現在版から目標版まで全部、原文を読む** (CHARTER §4)。要約や見出しで判断しない。
   複数マイナーを跨ぐものは、間のバージョンのノートも読む。
4. 更新 PR は **1 PR 1 コンポーネント**。ブランチは `autopilot/P-0029-<target-id>`
   (`project/*` は heart が予約している名前空間 — `ops/heart/reconcile.py` が
   `project/<id.lower()>` を作る。混ぜない)。**origin/main から切る**。
   PR 本文は日本語で、変更点 / 検証したこと / **ロールバック手順** / このプロジェクト id を必ず書く
   (CHARTER §4 共通ルール)。DB を持つもの (coder-postgres 等) のロールバックには
   「コードは戻るがスキーマは戻らない」を明記する。
5. PR 作成は `POST /repos/hikuohiku/homelab/pulls` を curl で。返ってきた番号を sweep 表に書く。
6. `high` 相当 (メジャー更新、breaking change 宣言あり、データを失いうる) に当たったら
   **PR を出さず、sweep 表に「据え置き + 理由 + 必要な前段」**を書く。CHARTER §4「high を戻せる形に落とす」。
   判断を人間に投げない。

### 実装上の罠 (踏むと 1 セッション無駄になる)

- **セッション終了時に HEAD を `project/p-0029` に戻すこと。** wrapper (`ops/runner/runner.py` の
  `push_if_committed`) はセッション末に `git push origin HEAD:project/p-0029` を無条件で打つ。
  更新ブランチに HEAD を置いたまま終えると、その内容がプロジェクトブランチに push される。
  → **更新ブランチは `git worktree` で作り、`/work/repo` の HEAD を動かさない**のが安全:
  ```bash
  WT=$(mktemp -d)
  git fetch --quiet origin main
  git worktree add "$WT" -b autopilot/P-0029-<id> origin/main
  # $WT で編集・commit
  git -C "$WT" push -u origin autopilot/P-0029-<id>
  git worktree remove "$WT"
  ```
- 一時ファイルは `mktemp` を使う。固定パス `/tmp/...` は前セッションの残骸を拾う (実測済み、substrate)。
- 更新 PR が複数同時に開くので、**全 PR が `ops/inventory.json` を触る**。エントリごとに行が離れて
  いるため通常は自動マージされるが、conflict したブランチは `origin/main` に rebase し直す。
  `git push --force` は使わない — 作り直す。
- `kustomize` / `helm` / `terraform` はこのイメージに無い (`kubectl kustomize` はある)。
  レンダリングの妥当性と削除検出 (manifest-diff) は **CI に任せる**。手元で確かめようとして溶かさない。

## やらないこと

- **`policy: "manual"` / `"pinned"` の 7 件** (`argocd-chart` / `immich-postgres` /
  `coder-workspace-image` / `nixpkgs` / `tf-provider-proxmox` / `claude-code-cli` /
  `autopilot-base-image`) **の更新 PR。** spec が `policy:auto` に限定している。
  調査ついでに気づいたことは PROGRESS.md の「発見」に 1 行書くだけにする。
- **`ops/inventory.json` の構造変更** (エントリの追加・削除、policy の変更、note の書き換え)。
  今回触ってよいのは更新した target の `current` だけ。監視対象の見直しは別の論点。
- **出した PR を merge すること・auto-merge を有効にすること。** DoD は「PR を出す」まで。
  CODEOWNERS 保護に当たる 17 件はそもそも人間レビュー必須で、抜け道を探さない
  (自分の強制装置を自分で merge しない — 設計決定 #7)。
- **実機確認 (`kubectl` でクラスタを見に行く、ArgoCD の sync を待つ)。** merge されていない変更は
  クラスタに存在しない。反映後の確認は別サイクル。
- **`ops/CHARTER.md` / `ops/memory/` の改訂。** 「作法が memory に無い」ことに気づいても、
  ここで直さない (consolidation の領分)。PROGRESS.md の「発見」に書いて次に渡す。
- **1 PR に複数コンポーネントを詰めること。** 「同じ python:3.14-alpine だからまとめて」もしない —
  mirrors (同じ target の別ファイル) を揃えるのは同じ PR、**別 target は別 PR**。
- **`ops/backlog.json` / `ops/state.json` / journal の更新。** heart の領分 (worker.md「ops/ の帳簿を触らない」)。

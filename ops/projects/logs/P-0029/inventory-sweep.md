# P-0029 — inventory sweep (policy:auto 全 31 対象の上流調査)

調査日: **2026-08-10**（すべて同日の一次情報。API 応答は当日のもの）
調査対象: `ops/inventory.json` の `policy: "auto"` の **31 件**（全 38 件中。manual/pinned の 7 件は
[PROJECT.md「やらないこと」](PROJECT.md) により対象外）。

## この環境から到達できた一次情報

すべて 2026-08-10 に実測（HTTP 200 を確認）。要約や第三者サイトは使っていない。

| 情報源 | 用途 |
|--------|------|
| `https://api.github.com/repos/<owner>/<repo>/releases?per_page=100` | GitHub リリース一覧（tag / published_at / prerelease / draft） |
| `https://api.github.com/repos/<owner>/<repo>/releases/latest` | 「最新リリース」の確定（floating major tag に騙されないため） |
| `https://api.github.com/repos/<owner>/<repo>/releases/tags/<tag>` | リリースノート原文 |
| `https://api.github.com/repos/<owner>/<repo>/pulls/<n>[/files]` | 変更の実体（PR 本文・diff。要約で判断しないため） |
| `https://hub.docker.com/v2/repositories/<repo>/tags?page_size=100&ordering=last_updated` | Docker Hub 実タグ一覧 |
| `https://hub.docker.com/v2/repositories/<repo>/tags/<tag>` | 特定タグの実在確認（200/404） |
| `https://ghcr.io/v2/<repo>/manifests/<tag>`（`ghcr.io/token` で取得した token 付き） | GHCR 実イメージの実在確認 |

**tailscale 系の裏取り方針**（inventory の `note` が指定しているもの）: chart index
(`pkgs.tailscale.com` / `artifacthub.io`) は使わず、**ghcr.io / Docker Hub の実イメージタグの存在**で
判断する。git tag があってもイメージが無い実例が過去にある（run #41）。今回まさに同じ形を踏んだ（後述）。

## 結論サマリ

- **更新 PR を出したもの: 1 件**（`external-secrets-chart` 2.8.0 → 2.9.0）
- **調べて据え置き: 30 件**
  - 上流が現在版と同一（＝既に最新）: 26 件
  - 新しい上流タグはあるが `note` の除外規定 / 配布物不在で上げられない: 4 件
    （`tailscale-operator-chart` / `k8s-nameserver` / `coder` / `gha-setup-helm-version`）

**「調べて据え置いた」と「調べていない」は下表で区別できる。** 未調査の行は 1 つも無い。

## sweep 表

| # | 対象 (id) | 現在版 | 上流最新 | 更新可否と根拠 | PR |
|---|-----------|--------|----------|----------------|-----|
| 1 | `dex-chart` | 0.24.1 | `dex-0.24.1` (2026-05-28) | **据え置き（既に最新）**。`releases?per_page=100` の先頭が `dex-0.24.1`、次が `dex-0.24.0` (2025-09-01)。差分なし | — |
| 2 | `external-secrets-chart` | 2.8.0 | `helm-chart-2.9.0` / `v2.9.0` (2026-08-07〜08) | **更新した**。chart version と appVersion が完全一致する運用（inventory note）なので minor 更新 1 ホップ。リリースノート原文を読了、breaking change 宣言なし。詳細は下の「読んだリリースノート」節 | [#428](https://github.com/hikuohiku/homelab/pull/428) |
| 3 | `immich-chart` | 0.13.1 | `immich-0.13.1` (2026-07-03) | **据え置き（既に最新）**。releases 先頭が `immich-0.13.1`、次が `immich-0.13.0` (同日) | — |
| 4 | `tailscale-operator-chart` | 1.98.9 | git tag は `v1.102.2` (2026-08-04)、**配布イメージは `v1.98.9` が最新** | **据え置き（配布物が無い）**。`hub.docker.com/v2/repositories/tailscale/k8s-operator/tags/v1.98.10` = **404**、`/v1.102.2` = **404**。GHCR も `ghcr.io/v2/tailscale/k8s-operator/manifests/v1.98.10` = **404**、`/v1.102.2` = **404**（`/v1.98.9` は 200）。Docker Hub の `stable` タグは 2026-07-17 更新で `v1.98.9` と同時刻＝ `stable` は今も v1.98.9 を指す。**note が指定するとおり git tag ではなく実イメージで判定**した結果、上げ先が存在しない。run #41 と同じ形の再発 | — |
| 5 | `vaultwarden` | 1.37.1-alpine | `1.37.1` (2026-07-29) | **据え置き（既に最新）**。releases 先頭が 1.37.1。`note` が名指しする 1.37.0 の alpine ビルド破損は**その次の 1.37.1 が現在版**なので既に回避済み。#49 の当事者だが今回上げるものは無い | — |
| 6 | `coder` | v2.35.3 | stable = **v2.35.3**、mainline = v2.36.0 (2026-08-04) | **据え置き（既に最新の stable）**。`note` の「stable チャンネルを追う（mainline の v2.36.0 系は避ける）」に従い、リリース本文で channel を判定した: **v2.35.3 の本文冒頭が `> ## Stable (since August 4, 2026)`**、v2.36.0 の本文は `> This is a mainline Coder release.`（BREAKING CHANGES 節あり: Dynamic client registration disabled by default）。v2.35.x 系で v2.35.3 より新しいものは無い。**自分の足場（開発環境）**なので mainline には触れない | — |
| 7 | `coder-postgres` | 17.10 | 17.10 (2026-08-05) | **据え置き（既に最新の 17 系）**。`hub.docker.com/.../library/postgres/tags/17.11` = **404**。18.4 / 19beta2 は存在するが **17→18 はメジャー＝データ移行が要る**（inventory note が「必ず人間へ」と明記）ため対象外。mirrors: `apps/coder/restic-backup-cronjob.yaml` の pg-dump initContainer も同タグだが、上げるものが無いので変更なし | — |
| 8 | `immich-server` | v3.1.0 | `v3.1.0` (2026-07-29) | **据え置き（既に最新）**。releases 先頭が v3.1.0（次は v3.0.3）。`immich-machine-learning` と同一版を維持 | — |
| 9 | `immich-machine-learning` | v3.1.0 | `v3.1.0` (2026-07-29) | **据え置き（既に最新）**。#8 と同一リポジトリ・同一版。`ops/check_version_sync.py` の GROUP「immich server / machine-learning tag」が一致を検証済み（実行して ok） | — |
| 10 | `immich-valkey` | 9.1.1-alpine | `9.1.1-alpine` (2026-07-22) | **据え置き（既に最新）**。`valkey/valkey/tags/9.1.2-alpine` = **404**。tags 一覧でも 9 系の最新は 9.1.1。`unstable-*` は追わない | — |
| 11 | `busybox` | 1.38.0 | `1.38.0` (2026-07-31) | **据え置き（既に最新）**。`library/busybox/tags/1.39.0` = **404**。mirrors 3 ファイル（vaultwarden/coder/immich）とも 1.38.0 で一致（check_version_sync.py ok） | — |
| 12 | `k8s-nameserver` | v1.98.9 | 配布イメージの最新は `v1.98.9` | **据え置き（配布物が無い）**。`tailscale/k8s-nameserver` の Docker Hub タグも operator と完全に同じ並び（`v1.98.10` = 404、`v1.102.2` = 404、stable = 2026-07-17）。**#4 と必ず同じ `vX.Y.Z` で揃える**規定があり、片方だけ動かす選択肢は無い | — |
| 13 | `gha-actions-checkout` | v7 | `v7.0.1` (2026-07-20) | **据え置き（既に最新メジャー）**。この repo は major floating tag (`@v7`) で pin する運用。`releases/latest` = v7.0.1 で v8 系は無い。`@v7` は v7.0.1 を指すので実効的に最新。mirrors 4 ファイル（ci / direct-push-guard / release-image / build-autopilot-image）とも v7（check_version_sync.py ok）。CODEOWNERS `/.github/` 保護対象 | — |
| 14 | `gha-azure-setup-helm` | v5 | `v5.0.1` (2026-06-23) | **据え置き（既に最新メジャー）**。`releases/latest` = v5.0.1、v6 系は無い。CODEOWNERS `/.github/` 保護対象 | — |
| 15 | `gha-hashicorp-setup-terraform` | v4 | `v4.0.1` (2026-05-12) | **据え置き（既に最新メジャー）**。`releases/latest` = v4.0.1、v5 系は無い。CODEOWNERS `/.github/` 保護対象 | — |
| 16 | `gha-cachix-install-nix-action` | v31 | `v31.11.0` (2026-07-15) | **据え置き（既に最新メジャー）**。`releases/latest` = v31.11.0 で v32 系は無い（`v31` の floating tag 自体は 2025-03-10 のままだが、指す先は v31 系の最新）。CODEOWNERS `/.github/` 保護対象 | — |
| 17 | `gha-cachix-cachix-action` | v17 | `v17` (2026-03-18) | **据え置き（既に最新メジャー）**。`releases/latest` = v17、v18 系は無い。CHARTER §5.4（release-image.yml は CI 非カバレッジ）対象だが上げるものが無い。CODEOWNERS `/.github/` 保護対象 | — |
| 18 | `gha-softprops-action-gh-release` | v3 | `v3.0.2` (2026-07-13) | **据え置き（既に最新メジャー）**。`releases/latest` = v3.0.2、v4 系は無い。CHARTER §5.4 対象。CODEOWNERS `/.github/` 保護対象 | — |
| 19 | `gha-nix-installer-action` | v22 | `v22` (2026-03-29) | **据え置き（既に最新メジャー）**。`releases/latest` = v22、v23 系は無い。CHARTER §5.4 対象。CODEOWNERS `/.github/` 保護対象 | — |
| 20 | `ops-health-reporter-image` | 3.14-alpine | `3.14-alpine` → 3.14.7 (2026-08-05) | **据え置き（pin が floating minor tag）**。`library/python/tags/3.15-alpine` = **404**（3.15 系は未リリース）。`3.14-alpine` は 3.14 系の patch を自動追従する tag なので、3.14 内の patch 更新でリポジトリを触る必要は無い。上げるとしたら 3.15 が出たときの minor 更新 | — |
| 21 | `pvc-usage-reporter-image` | 3.14-alpine | 同上 | **据え置き（#20 と同じ理由）**。mirrors 4 ファイル（immich/coder/vaultwarden の pvc-usage-cronjob + vaultwarden の restic-backup-cronjob）とも 3.14-alpine で一致（check_version_sync.py ok）。mirrors 経由で `apps/*/restic-*.yaml` に触るため CODEOWNERS 保護対象 | — |
| 22 | `ops-dashboard-image` | 3.14-alpine | 同上 | **据え置き（#20 と同じ理由）**。`apps/ops-dashboard/deployment.yaml:101` が 3.14-alpine で一致 | — |
| 23 | `coder-workspace-home-backup-image` | 3.14-alpine | 同上 | **据え置き（#20 と同じ理由）**。`apps/coder/workspace-home-backup-cronjob.yaml:302` が 3.14-alpine で一致。ファイル名が `*backup*.yaml` なので CODEOWNERS 保護対象 | — |
| 24 | `gha-setup-helm-version` | v3.21.3 | v3 系最新 = **v3.21.3** (2026-07-09)。repo 全体の最新は v4.2.3 | **据え置き（既に最新の v3 系。v4 は blocked）**。`releases?per_page=100` を v3 系で絞ると先頭が v3.21.3。`note` の通り **v4 系は azure/setup-helm が README で v3 のみ公式サポートと明言しているため blocked（T-0118）**。v3 系内では上げ先が無い。ci.yml の 2 箇所とも v3.21.3（check_version_sync.py ok）。CODEOWNERS `/.github/` 保護対象 | — |
| 25 | `kustomize-binary` | v5.8.1 | `kustomize/v5.8.1` (2026-02-09) | **据え置き（既に最新）**。`releases/latest` = `kustomize/v5.8.1`。ci.yml の 2 箇所とも一致（check_version_sync.py ok）。CODEOWNERS `/.github/` 保護対象 | — |
| 26 | `terraform-binary` | 1.15.8 | `v1.15.8` (2026-07-08) | **据え置き（既に最新）**。`releases/latest` = v1.15.8。`terraform/proxmox/providers.tf` の `required_version = "~> 1.15"` の範囲内。CODEOWNERS `/.github/` 保護対象 | — |
| 27 | `vaultwarden-restic-image` | 0.19.1 | `v0.19.1` (2026-07-05) | **据え置き（既に最新）**。releases 先頭が v0.19.1（次は v0.19.0, 2026-06-09）。`apps/*/restic-*.yaml` で CODEOWNERS 保護対象 | — |
| 28 | `coder-postgres-restic-image` | 0.19.1 | 同上 | **据え置き（#27 と同一の上流・同一版）**。CODEOWNERS 保護対象 | — |
| 29 | `immich-restic-image` | 0.19.1 | 同上 | **据え置き（#27 と同一の上流・同一版）**。CODEOWNERS 保護対象 | — |
| 30 | `coder-workspace-home-restic-image` | 0.19.1 | 同上 | **据え置き（#27 と同一の上流・同一版）**。4 ファイル 6 箇所すべて 0.19.1 で一致（check_version_sync.py の GROUP「restic/restic backup CronJob image tag」ok）。`*backup*.yaml` で CODEOWNERS 保護対象 | — |
| 31 | `syncthing` | 2.1.3 | `v2.1.3` (2026-08-05) | **据え置き（既に最新）**。releases の非 prerelease 先頭が v2.1.3。より新しいのは `v2.1.3-rc.*` 系のみ（prerelease、追わない）。タグに `v` プレフィックスが付かない（`syncthing/syncthing:2.1.3`）のは inventory note の実測どおり | — |

## 読んだリリースノート（更新した 1 件）

### `external-secrets-chart` 2.8.0 → 2.9.0

現在版から目標版まで **1 ホップ**（`helm-chart-2.8.0` (2026-07-18) の次が `helm-chart-2.9.0` (2026-08-08)。
間のバージョンは無い）。chart version と appVersion が完全一致する運用なので、appVersion 側の
`v2.9.0` (2026-08-07) のリリースノート**全文**を読んだ。

- **BREAKING CHANGES 節は無い。** 大半が依存 bump（grpc-go / golang.org/x/text の CVE 修正、
  GitHub Actions の dependabot 更新）、provider 個別修正（conjur / akeyless / aws / 1Password）、
  e2e テスト整備。この homelab は **Doppler provider のみ**（`apps/external-secrets/cluster-secret-store.yaml`）
  なので、provider 個別修正は該当しない。
- **API に触る変更は 2 系統あったので diff まで読んだ**（要約では判断しない、CHARTER §4）:
  - **#6798 `fix(api): stop defaulting optional ExternalSecret strategy fields`** —
    CRD から `conversionStrategy` / `decodingStrategy` / `metadataPolicy` /
    `valuesDecodingStrategy` の `kubebuilder:default` マーカーを削除する変更。PR 本文いわく
    「API server が値を注入するせいで ArgoCD が永久に diff と戦う」(#6398/#6797) の修正で、
    **GitOps 側にとっては改善方向**。この repo は
    `apps/external-secrets/tailscale-oauth-external-secret.yaml` で 3 フィールドを明示的に
    書いており（まさに PR が言う「ユーザに強いていた回避策」の形）、**明示値はそのまま残るので
    挙動は変わらない**。既存オブジェクトも壊れない（default の削除であって必須化ではない）。
  - **#6730 / #6735 `template abuse for secret values`（+ validation の前倒し）** —
    `spec.target.template.templateFrom[].target` の値を Secret 向けには
    `""` / `Data` / `Annotations` / `Labels` に制限する新規 validation（admission webhook +
    controller 側の defense in depth）。diff（`externalsecret_validator.go` の
    `ValidateSecretTemplateFromTargets`）を直接読んで対象フィールドを確定した。
    **`apps/` 配下に `templateFrom` の使用は 1 件も無い**（`grep -rn templateFrom apps/` が空）。
    唯一 `template:` を使う `apps/argocd/dex-client-secret-external-secret.yaml` も
    `template.metadata.labels` だけで `templateFrom` を持たないため、この validation に当たらない。
  - **#6799 `fix enable cache being removed on installCRD=false`** — 条件が `installCRD=false`。
    この repo は `valuesInline: installCRDs: true`（`apps/external-secrets/kustomization.yaml`）
    なので該当しない。
- **CRD の render 差分**は CI の `manifest-diff` job（`ops/check_manifest_deletions.py`, T-0036）に
  委ねる。手元に `helm` / `kustomize` バイナリが無く（`kubectl kustomize` は helm chart を
  引けない）、CHARTER §5.3 の「レンダリングされた実体を見る」は CI が既に答えを持っている。

## 補足: CODEOWNERS 保護と auto-merge

31 件のうち **17 件**が CODEOWNERS（`/.github/`, `apps/*/restic-*.yaml`, `apps/*/*backup*.yaml`）に
触るため、更新 PR を出しても auto-merge できず人間レビューが要る（上表の各行に明記した）。
ただし**今回そのすべてが「既に最新」で据え置き**になったため、この制約が実際に効いた PR は無い。
唯一出した `external-secrets-chart` の PR は `apps/external-secrets/kustomization.yaml` と
`ops/inventory.json` のみを触り、保護パスに当たらない。

（DoD は「PR を出す」までで、merge は別サイクル。auto-merge の有効化はこのプロジェクトでは行わない。）

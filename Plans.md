# GitHub Copilot 日本語レビュー設定 Plans.md

作成日: 2026-05-21

関連: #42 (GitHub Copilot PR レビューを日本語で投稿するよう設定)

---

## 概要

GitHub Copilot の PR レビューコメントが英語で投稿されるため、日本語で投稿するようカスタム指示を追加する。

---

## Phase 1: Copilot カスタム指示

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| 1.1 | `.github/instructions/copilot.instructions.md` を作成: 日本語レビュー指示 | ファイルが存在し、日本語でレビューする指示が含まれる | - | cc:完了 |
| 1.2 | PR 作成・マージ | PR がマージされている | 1.1 | cc:完了 [PR #43] |

---

# Proxmox 棚卸し / pbs 管理方針 Plans.md

作成日: 2026-06-21

## 概要

Proxmox 上の停止中 VM/LXC を棚卸しで削除し、Terraform 管理外でドリフトしていた
pbs (qemu/112) の扱いを決定する。

## Phase 1: 棚卸し・pbs 管理方針

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| A | 停止中 VM/LXC の棚卸し・削除（9台） | 現存は vaultwarden(100)/syncthing(101)/docker(106)/pbs(112)/node01(113) のみ | - | 完了 [PR #45] |
| B | pbs を手動管理（Terraform 管理対象外）として明示 | pbs.tf.ignore に注記追記 / README・CLAUDE.md に明記 / Proxmox の `terraform` タグ削除 | A | 完了 [PR #45] |
| C | IaC 管理外の空 namespace 削除（ente / nextcloud / vaultwarden） | 3 namespace を削除（nextcloud は 50Gi データ含め破棄合意済み）/ repo の nextcloud 定義（kustomization 行・apps/nextcloud/・dex OIDC client）を整理 | - | 完了 [PR #46] |

---

# LXC → k8s 移行 Plans.md

作成日: 2026-06-21

## 概要

Proxmox LXC で手動運用中のサービスを k8s (ArgoCD) へ移行する。manifest は helm chart を使わずゼロスクラッチで手書きする方針。immich が先行移行済み。

## Phase 1: 移行バックログ

| Task | 内容 | DoD | Depends | Status |
|------|------|-----|---------|--------|
| M0 | docker VM(106) 削除（未使用） | qemu/106 削除 / Tailscale `hikuo-homedocker` 削除 | - | TODO（ユーザー手動） |
| M1 | vaultwarden を k8s 移行（手書き manifest） | apps/vaultwarden/ 作成・登録 / Doppler `VAULTWARDEN_ADMIN_TOKEN` 登録 / 旧 LXC tailscale 退避 / /data 移行 / 検証 | - | 完了 [PR #47]（ciphers 781 件移行・ログイン確認済み。旧 LXC 100 破棄はユーザー手動待ち） |
| M2 | syncthing を k8s 移行 | 後回し（~100G・P2P 特性のため移行可否も含め保留） | - | 保留 |

> 補足: k8s への書き込み操作（移行時の scale/cp/exec 等）に伴い、`.claude/settings.json` の `permissions.ask` に `Bash(kubectl:*)` を追加し、kubectl は人間レビュー（ask）付きで実行する方針に変更（CLAUDE.md 反映済み）。

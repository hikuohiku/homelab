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
| A | 停止中 VM/LXC の棚卸し・削除（9台） | 現存は vaultwarden(100)/syncthing(101)/docker(106)/pbs(112)/node01(113) のみ | - | 完了 |
| B | pbs を手動管理（Terraform 管理対象外）として明示 | pbs.tf.ignore に注記追記 / README・CLAUDE.md に明記 / Proxmox の `terraform` タグ削除 | A | cc:完了（タグ削除はユーザー手動実施待ち） |

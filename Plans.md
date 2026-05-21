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

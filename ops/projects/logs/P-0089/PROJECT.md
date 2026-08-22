# P-0089 — Mission Control にページアイコン (favicon) を足す

## 目的

人間の依頼 (2026-08-22)。ブラウザタブで Mission Control (ops-dashboard) が他のタブと
見分けが付くように、管制室らしい favicon を足す。Next.js App Router のファイル規約
(`src/app/icon.svg`) に乗せ、ダークタブでも視認できる配色にする。

## 受入チェックリスト

initializer 実測 (2026-08-22、`project/p-0089` の checkout、リポジトリルートから):
**1 項目とも現時点で failing**。

- [ ] `test -f apps/ops-dashboard/app/src/app/icon.svg`
  — App Router 規約の置き場所にアイコンファイルが存在すること。実測 rc=1
    (`apps/ops-dashboard/app/src/app/` 配下は layout.tsx / page.tsx / globals.css /
    api/ のみで icon.svg は無い)。

## 設計方針

### 前提 (initializer が 2026-08-22 に実読した。調べ直さなくてよい)

- アプリの実体は `apps/ops-dashboard/app/`。**App Router** 構成
  (`src/app/layout.tsx`、package.json は next 16.3.2 / react 19.2.8)。
- `layout.tsx` は `export const metadata` (title / description) を持つが、
  **icon 用の metadata 追記は不要**。App Router のファイル規約では
  `src/app/icon.svg` を置くだけで Next.js が `<link rel="icon">` を自動生成する。
  変更は新規ファイル 1 つの追加だけで完結する。
- リポジトリ内に既存の icon/favicon 資産は無い (`glob apps/**/icon.svg` = 0 件)。
  流用元は無いので新規に書く。
- spec 制約: **P-0088 が同じアプリを並行で触っている**ため page.tsx 等の共有ファイルには
  触れない (衝突回避)。

### 方針

1. `apps/ops-dashboard/app/src/app/icon.svg` を新規作成する。正方形 viewBox
   (例: 32×32)、単独ファイルで完結させる (外部フォント・外部画像に依存しない)。
2. モチーフは「管制室」らしさ — レーダー (同心円 + スイープ線) や信号波形など、
   autopilot の監視画面を想起させるもの。
3. 配色はダークタブでの視認性を最優先する。暗い線だけの透過背景は避け、
   明るいアクセント色 (緑〜シアン系の管制室カラー) を主役にし、塗り背景を持たせて
   明るいテーマのタブバーでも判別できるようにする。

## やらないこと

- **deployment.yaml へ触らない** — digest 反映は人間側の仕事 (spec 明記)
- page.tsx / globals.css 等の共有ファイルの編集 — P-0088 との衝突回避 (spec 明記)
- layout.tsx の metadata 変更 — ファイル規約で不要なため。どうしても必要と判明した時点で
  最小限を再検討する
- favicon 以外の UI 改善・OGP 画像 (opengraph-image 等) — 1 PR 1 論点

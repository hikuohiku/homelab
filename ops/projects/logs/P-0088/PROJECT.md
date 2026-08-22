# P-0088 — Mission Control に autopilot の構成図ページを足す

## 目的

人間の依頼 (2026-08-22)。heart-and-projects の構成 — heart のビート / curriculum→採択ゲート→
予告→runner→独立レビュー→merge→soak というプロジェクトの一生 / 状態層 (ops-state・main・PVC) /
人間との接点 — を、Mission Control 内の 1 ページで図解する。新参者 (未来の自分を含む) が
5 分で全体像を掴めることが目的。実装は `apps/ops-dashboard/app` 内で完結させる。

## 受入チェックリスト

initializer が 2026-08-22 に実測した結果、**2 項目とも現時点で failing**
(ブランチ `project/p-0088` の checkout、リポジトリルートから実行)。

- [ ] `test -f apps/ops-dashboard/app/src/app/architecture/page.tsx`
  — 構成図ページが Next.js App Router の正しい位置に存在すること (`src/app/architecture/`
    配下に page.tsx を置くと `/architecture` ルートになる)。実測 rc=1 (ファイル未存在)。
- [ ] `grep -q 'architecture' apps/ops-dashboard/app/src/app/page.tsx`
  — メインページから構成図ページへの導線 (nav リンク等、"architecture" を含む href)
    が張られていること。実測 rc=1 (`page.tsx` に "architecture" の文字は無い。現状の
    masthead nav は view 切替 button 群のみで、別ページへの遷移先は無い)。

## 設計方針

### 前提（initializer が 2026-08-22 に実読した。調べ直さなくてよい）

- **`apps/ops-dashboard/app` は Next.js 16 App Router**。`src/app/` 配下の
  `page.tsx` がルート。新ページは `src/app/architecture/page.tsx` を置くだけで生える。
- **スタイルは `src/app/globals.css` の 1 枚に集約**されている。Tailwind も CSS modules も無く、
  テーマは CSS 変数 (`--night/--deck/--panel/--line/--ink/--muted/--signal/--amber/--danger/
  --blue`) で定義済み。**依存パッケージは next/react/react-dom のみ** (`package.json`) で、
  図示ライブラリは無い → 図は手書き SVG + CSS で描くしかない (spec も外部 CDN 禁止)。
- メインページ (`page.tsx`) は `"use client"` の 1 ファイル構成。masthead nav
  (page.tsx:318-322) は view 切替の button 群で、`<a>` によるページ遷移は identity link
  (page.tsx:314) のみ。
- `layout.tsx` が全ページ共通で `FeedbackForm` を注入し metadata を設定する。新ページにも
  自動で付くので、新ページ側でやることは本文だけ。
- **検証コマンド**: `npm ci && npm run lint && npm test`。lint は `tsc --noEmit`、test は
  node:test (`tsx --test tests/*.test.ts`、snapshot/transcript のロジックテスト)。
  静的ページ追加は既存テストの対象外だが、壊してはならない。
- **アプリ README に二段階リリースが明記されている**: main merge → SHA tag で build → digest
  実測 → Deployment の image を digest pin するのは**別 PR**。だから今回
  `apps/ops-dashboard/deployment.yaml` には触らない (spec の注意書きと一致)。
- 図の中身の情報源は `ops/VISION.md`「今の器」節と CLAUDE.md が正確。heart のビート
  (120 秒ごとの観測と状態機械、LLM を呼ばない) / プロジェクトの一生 / 記憶 (ops-state・
  main・PVC) / 人間との接点 (Discord=push 型・ダッシュボード=pull 型・issue #56 veto) の
  4 領域で構成図が作れる。

### 決めてあること（この方針で作る。変えるなら理由を PROGRESS.md に書く）

1. **ページは静的サーバーコンポーネント**。`"use client"` 不要、データ fetch 無し、
   API route 追加も無し。ビルド時に確定する HTML/SVG だけで完結させる。
2. **図はインライン SVG**（ノード・矢印・日本語ラベル）。レイアウト補助に CSS を併用してよいが
   ライブラリは使わない。SVG は `role="img"` + `title`/`aria-label` を付け、アニメーションを
   入れる場合は globals.css 既存の `prefers-reduced-motion` 対応に乗る形にする。
3. **スタイルは `globals.css` の末尾にコメント区切りで追記する**（`/* --- 書き置きフォーム ... --- */`
   セクションの前例倣い）、配色は既存 CSS 変数を使う。新規 CSS ファイルは作らない。
4. **ナビリンクは masthead に `<a href="/architecture">構成図</a>` 形で足す**。verify 2 が
   `page.tsx` 中の "architecture" 文字列を grep するので、href にこの語を含めることが必須。
   `next/link` を使うか素の `<a>` かは worker の裁量（どちらも verify は満たす）だが、
   view 切替 button とは別物なので見た目で区別が付くようにすること。
5. **構成図の内容は VISION.md 準拠の 4 領域**: (a) heart のビート周期と観測対象、
   (b) プロジェクトの一生 (curriculum→採択ゲート→予告→runner→独立レビュー→merge→soak)、
   (c) 状態の置き場所 (ops-state ブランチ / main / PVC)、(d) 人間との接点 (Discord push /
   ダッシュボード pull / issue #56 の拒否権)。各領域に 1 行程度の説明文を添え、
   「5 分で全体像を掴む」ことを基準に情報量を絞る。
6. **構成図ページからメインページへの戻りリンクを付ける**。片道だけの導線はダッシュボードとして
   不自然なため。

### ロールバック

追加のみの変更（新 page.tsx + globals.css への追記 + page.tsx への nav リンク 1 行）。
revert すれば消えて元に戻るだけで、データ・API・他ページへの影響はない。

## やらないこと

- **`apps/ops-dashboard/deployment.yaml` その他 manifest (Dockerfile / kustomization /
  rbac 等) は一切触らない。** イメージの digest 反映とデプロイは merge 後に人間側が行う
  （README の二段階リリース）。spec の明示的な注意書き。
- **既存 view (live/projects/attention) や masthead nav button 群のリファクタリング**。
  nav を Link 化して統一する等の整理は誘惑だが、1 PR 1 論点。リンク 1 本の追加に留める。
- **外部 CDN・図示ライブラリの導入禁止**。package.json に依存を足さない (react-flow 等も NG)。
- **データ取得・動的要素なし**。ops-state / PVC / K8s API を読むページにはしない。静的 1 枚。
- **英語版・i18n・印刷最適化**等はスコープ外。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。autopilot が直接 main に
  push する帳簿でコンフリクトの元 (CLAUDE.md)。気づいたことは PROGRESS.md に書いて次に渡す。

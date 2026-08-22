# P-0088 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### session1 (2026-08-22)

**やったこと**: 受入 2 項目を実装し、両方 green を実測。

- `apps/ops-dashboard/app/src/app/architecture/page.tsx` を新設。静的サーバーコンポーネント
  (`"use client"` なし・fetch なし) で、インライン SVG の図を 4 枚:
  (01) heart のビート — 観測→状態機械→Job 起動の循環、120 秒・LLM 呼ばない
  (02) プロジェクトの一生 — 立案→採択ゲート→予告→実行→レビュー→取り込み→様子見→納品の
  8 段階を蛇行レイアウトで (下段は右から左へ流れる。矢印と番号 01-08 で向きを明示)
  (03) 状態の置き場所 — ops-state / main / PVC の 3 層バンド
  (04) 人間との接点 — Discord push / Mission Control pull / issue #56 veto の 3 レーン
  いずれも `role="img"` + `<title>` + aria-label 付き。配色は globals.css 既存変数のみ。
- masthead nav に `<a class="nav-page" href="/architecture">構成図</a>` を追加
  (page.tsx)。view 切替 button と区別するためシグナル色 + ↗ グリフ。戻りリンクは
  構成図ページ側の `.arch__back` で実装。
- スタイルは globals.css 末尾に `/* --- 構成図ページ --- */` 区切りで追記
  (.arch* 名前空間、既存クラスとの衝突なし)。980px / 640px ブレークポイントにも対応追記。

**検証 (すべて自分で回した)**: verify 2 項目 OK / `npm ci && npm run lint && npm test`
(5 tests pass) に加え `npx next build` も通し、`/architecture` が ○ (Static) prerender
されることを確認。

**分かったこと / 罠**:

- PROJECT.md の「決めてあること」が詳細すぎるほど正確だった。座標手打ち SVG は
  「隣接ボックスへの矢印が食い込む」「蛇行 2 段目の x 座標を添字演算で出そうとして壊す」
  2 箇所でミスした → **座標はループで賢くやらず、上段/下段とも全ボックスを明示的に並べる**
  のが正解だった (修正済み)。
- SVG `<marker>` の id はページ内で一意にする必要があるため、SVG ごとに
  `arr-beat` / `arr-life` / `arr-touch-*` と接頭辞を分けた。新規 SVG を足すときは要継承。
- next build はローカル検証コマンドの指定外だが静的ページ追加の実質的な受入になる
  (JSX/座標ミスは tsc では落ちない)。次セッションも触ったら build を回すこと。

**発見 (スコープ外、curriculum へ)**: 特になし。

**次のセッションへ**: 実装と検証は完了済み。残っているのはレビュー対応のみ。
もし指摘で図の中身をいじる場合は page.tsx 内の座標コメント (上段左→右 / 下段右→左)
を読んでから手を入れること。deployment.yaml には最後まで触っていない (merge 後に人間が
digest pin する)。

# P-9005 — 進捗

## s1 (2026-08-24, worker — 初回セッション)

### 前提: この仕様の要求は既に P-9003 で満たされている

- P-9003 (同一 spec の前回 dispatch) は実は成功しており、`f3ce65450` で実装 →
  PR #607 で main に merge 済み (2026-08-24)。`project/p-9005` は main HEAD
  (`6b565410d`) と同一内容で、ページ変更は既に入っている。
- つまり本ブランチで新たに書くコードは無い。session の役割は「要求が実際に満たされているか」
  を自分の手で再確認し、記録に残すこと。

### やったこと (検証はすべてこのセッションの手で実施)

1. **コード実読で実装の存在を確認**: `apps/ops-dashboard/app/src/app/page.tsx:224-231` に
   `agent?.resident` 分岐あり。resident 時は `<strong>常駐エージェントのため transcript
   表示なし</strong>` + `<span>稼働状態はエージェントカードに表示されます</span>`。
   Job 由来 agent 有り / agent 無しの従来表示は無変更。`git show f3ce65450` の diff と
   現在のワークツリーが一致することを確認。
2. **lint / test green**: `npm ci` → `npm run lint` (tsc --noEmit) exit 0 / `npm test`
   9 pass, fail 0。
3. **クライアント描画スモーク (jsdom + React 19 act) を自作して 3 分岐を実測**:
   - resident 選択時 → 「常駐エージェントのため transcript 表示なし」+「稼働状態は
     エージェントカードに表示されます」が出る
   - resident 選択時 → 待機文言「transcript 信号を待っています」「ファイルが作られると
     自動で表示します」が出ない
   - worker 選択時 → 従来文言が維持され、新文言が出ない
   全アサーション通過。スクリプトは `mktemp -d` 由来の名前で app 配下に置き、
   実行後 rm -rf (lint の前に必ず削除 — tsconfig include `**/*.tsx` の罠)。
4. ワークツリーはクリーン (node_modules は npm ci 由来で gitignore 対象、smoke dir は削除済み)。

### 分かったこと

- **P-9004 (常駐への transcript 表示追加) は main 未達**。`origin/project/p-9004` には
  initializer コミット 1 件 (2a6a57c59) のみで、P-9003 の PROGRESS に書かれていた
  「P-9004 merge 時に『transcript 表示なし』は要再検討」の前提は今も有効。
  現時点の文言は嘘になっていない。
- 本仕様の受入項目 2 つは P-9003 merge 済みコードで両方満たされている。verify 空の仕様
  につき完成宣言はしないが、実装すべき残りは無い。

### 次のセッションへの一言

- **追加実装は不要**。レビュー差戻りが来た場合のみ対応:
  - 文言への指摘なら、先に `origin/project/p-9004` の merge 状況を確認してから変えること
    (単独で変えると二度手間)。
  - それ以外の差戻りは、この PROGRESS と PROJECT.md の記録を根拠に「P-9003 で merge 済み
    の重複仕様」であることを説明する。

### 罠 (次回用)

- jsdom は `window.EventSource` を実装していない (undefined)。TranscriptViewer の effect が
  `new EventSource(...)` で落ちるので、stub クラスを `globalThis.EventSource` に定義すること。
- smoke dir は app 配下に置く (react-dom の module resolution)。`{"type":"module"}` の
  package.json が必要。`mktemp -d` の裸呼び出しでユニーク名を作り、最後に必ず rm -rf
  (残すと lint が tsx を拾って落ちる)。
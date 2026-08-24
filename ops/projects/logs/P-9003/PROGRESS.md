# P-9003 — 進捗

## s1 (2026-08-24, initializer)

### やったこと

- PROJECT.md と PROGRESS.md を作成して commit。実装は未着手。
- spec の verify は空 (`[]`, 所有者指示で省略) のため受入基準を why/dod から派生して
  PROJECT.md に列記。2 項目とも現状 failing をコード実読で確認済み
  (page.tsx:223-227 に resident 分岐が無いこと / snapshot.ts:57-69 が false 固定のまま)。

### 次のセッションへの一言

- 変更点は page.tsx の scope__empty (223-227 行付近) の分岐追加だけ。snapshot.ts は正なので触らない。
- 並走 `project/p-9002` (常駐 transcript 表示の追加) の merge 状況を確認してから文言を決めると良い。
  2026-08-24 時点で同ブランチにコミットは無い。

## s2 (2026-08-24, worker)

### やったこと

- page.tsx の `scope__empty` に `agent.resident` 分岐を追加 (PROJECT.md 設計方針 §2 の
  AgentCard 同様の様式)。文言は強調「常駐エージェントのため transcript 表示なし」/
  補足「稼働状態はエージェントカードに表示されます」。Job 由来・agent 無しの従来表示は無変更。
- 検証: `npm run lint` (tsc --noEmit) green / `npm test` green (fail 0)。
  加えて jsdom + React 19 `act` で Home をクライアント描画するスモークを作り
  **3 分岐を実測**: resident 選択時に新文言が出て待機文言・「ファイルが作られると自動で表示します」
  が出ないこと / worker 選択時と agent 無しで従来文言が出ること。全アサーション通過。
- PROJECT.md 受入チェックリスト 2 項目はこの実測で both passing のはず (判定は reviewer へ)。

### 分かったこと

- `origin/project/p-9002` は main と同位置で独自コミット無し (2026-08-24 実測)。
  文言合わせの必要は今回無かった。**P-9002 系 (台帳 P-9000/P-0317) が merge されたら
  resident の空状態文言は要再検討** — 常駐にも transcript が流れる設計に変わるため、
  「transcript 表示なし」が嘘になる可能性がある。
- TranscriptViewer は export されず unit test 射程外 (既知)。SSR (`renderToStaticMarkup`)
  では useEffect が走らず fetch モックも発火しないため Home 経由の描画確認は不可能。
  クライアント描画での実測が最短ルート。jsdom は app の依存に無いので smoke dir 内に
  `npm install --prefix` で一時導入した (package.json は汚さず、終わったら rm -rf)。

### 罠 (次のセッションへ)

- この環境の mktemp は非標準: テンプレート内スラッシュ不可 (`Invalid argument`) かつ
  `/tmp/opencode` は書き込み不可 (Permission denied)。`mktemp -d` 裸なら `/tmp/tmp.*` が取れる。
- tsx スクリプトを node_modules 探索圏外 (/tmp 直下) に置くと react-dom 解決で落ちる。
  app ディレクトリ内に置くこと。その際 smoke dir には `{"type":"module"}` の package.json が
  要る (無いと CJS 扱いで top-level await が構文エラー)。Node 22 は `globalThis.navigator`
  が getter のみなので `Object.defineProperty` で上書きする。
- React の act を使うと "not configured to support act(...)" 警告が大量に出るが実害なし
  (`IS_REACT_ACT_ENVIRONMENT` 未設定のため)。アサーション結果は正しく出る。

### 次のセッションへの一言

- レビュー差戻りがあればその解消を最優先。なければ本件は完了見込み。
- 差し戻しが文言への指摘の場合、P-9002 系の merge 状況を先に確認してから変えること
  (単独で変えると二度手間)。

## s3 (2026-08-24, worker)

### やったこと

- reviewer が verdict を書かなかったため差戻し事項は無し。s2 の実装 (`f3ce65450`, ワークツリー
  クリーンで同一内容) を**このセッションの手で再実測**した:
  - `npm ci` 後に `npm run lint` (tsc --noEmit) green / `npm test` green (9 pass, fail 0)。
  - jsdom + React 19 `act` のクライアント描画スモークを s2 の罠メモに従って再構築し、
    **3 分岐 10 アサーション全パス**: resident 選択時に「常駐エージェントのため transcript
    表示なし」+ 補足が出て待機文言・「ファイルが作られると自動で表示します」が出ない /
    `.resident-badge` がある / worker 選択時と agent 無しで従来文言が出る。
    スクリプトは使い捨て (rm -rf 済み、ワークツリーはクリーンを確認)。
- 受入チェックリスト 2 項目はこの実測で両方満たされている。判定は引き続き reviewer と CI へ
  (verify 空の仕様につき完成宣言はしない)。

### 分かったこと

- `origin/project/p-9002` は依然 main 同位置 (2026-08-24 再実測)。文言合わせの必要なし。
  P-9002 系 merge 時に文言要再検討、は引き続き有効。

### 罠 (次のセッションへ)

- s2 の罠メモに加えて: app/tsconfig.json の include が `**/*.tsx` を拾うため、smoke dir を
  app 配下に置いたまま `npm run lint` を回すと smoke スクリプトまで型検査されて落ちる。
  スモーク → lint の順でなく、**smoke dir は rm -rf してから lint** を回すこと。
- Home の import は jsdom グローバル設定後に行う (静的 import は hoist されるので
  dynamic import 推奨)。react 19 では `act` が react 本体から export されており
  `IS_REACT_ACT_ENVIRONMENT = true` を set すれば警告も消える。

### 次のセッションへの一言

- 実装・検証とも済んでおり、残作業は想定していない。差戻しが来た場合のみ対応
  (文言への指摘なら P-9002 系の merge 状況を先に確認)。

# P-9003 — 常駐エージェント選択時に「transcript 信号を待っています」と出る不整合を直す

## 目的

所有者の Telegram 明示依頼 (2026-08-24T19:35:11Z「採択ゲートの仕組みを取り払ったのでさっきの
タスク2件をもう一度dispatchしてください」)。前回 dispatch P-9001 (dispatch_id=d-5b629eb5dc3ffee8)
の再送。verify は所有者の指示により省略。

ops-dashboard のエージェントライブで常駐エージェント (autopilot-core / autopilot-heart) を選ぶと、
transcript が来る見込みが設計上ないのに「transcript 信号を待っています / ファイルが作られると
自動で表示します」と出る。待機ではないことが伝わる表示へ変える。

## 受入チェックリスト

spec の `verify` は空 (`[]`) — 所有者の指示により省略された (2026-08-24 の dispatch verify 廃止、
PR #604/#606 と同じ経緯)。機械検査コマンドが無いため、受入基準は why/dod から派生して列挙する。
initializer が 2026-08-24 に `project/p-9003` checkout のコード実読で現状を確認した結果、
**下記 2 項目とも現時点で failing**。

- [ ] 常駐エージェント (autopilot-core / autopilot-heart) を選んだとき、scope__empty の
  強調文言が「transcript 信号を待っています」にならないこと。
  実測: `apps/ops-dashboard/app/src/app/page.tsx:223-227` の空状態は agent がいれば
  resident か否かに関わらず待機メッセージを出す。
- [ ] 常駐エージェント選択時に「ファイルが作られる」ことを示唆する補足文言が出ないこと
  (「常駐エージェントのため transcript 表示なし」等、対象外であることが伝わる文言)。
  実測: page.tsx:226 が常に「ファイルが作られると自動で表示します」を出す一方、
  `apps/ops-dashboard/app/src/lib/snapshot.ts:57-69` は resident の transcriptAvailable を
  false に固定しており、ファイルは設計上作られない。

## 設計方針

前提は initializer が 2026-08-24 に実読済み。調べ直さなくてよい。

1. **変更点は page.tsx の空状態 1 箇所のみ**。`TranscriptViewer` 内の `scope__empty`
   (page.tsx:223-227) は `agent ? 待機メッセージ : Job 無しメッセージ` の 2 分岐で、
   resident の考慮が無い。ここに分岐を足すのが最小差分。
2. **分岐様式は AgentCard に倣う**。同ファイルの `AgentCard` (75, 83 行) は既に
   `agent.resident` で常駐バッジ・時刻表示を切り替えている。空状態も同じフラグで
   resident / Job 由来 agent 有り / agent 無しの 3 分岐にする (spec もこの様式を指示)。
   文言案は worker に任せるが、要件は「待っていないことが伝わる」こと。
3. **snapshot.ts は触らない**。resident の `transcriptAvailable: false` は正しい事実表現で、
   不整合は表示側だけ。transcript パイプラインも `parseAgentName` が Job 由来役割名
   (runner|reviewer|curriculum|critic|consolidation|chore)-<projectId>-aN しか解釈せず、
   常駐にはファイルが生えない (`src/lib/transcript.ts`)。CSS が必要になっても
   globals.css の最小追記に留める。
4. **並走プロジェクトへの配慮**。常駐組の transcript 表示そのものを追加する別件
   (spec 注記では P-9002。台帳では同趣向の P-9000 / P-0317 が採択、P-0318 は不採択) があり、
   そちらが merge されると常駐にも transcript が流れ文言が変わる可能性がある。
   2026-08-24 時点で `origin/project/p-9002` にコミットは無く main 未達。本件は完了までの
   間も誤解を招く待機表示を直す独立の価値があるので待たず、merge 状況を見て文言を合わせる
   判断は worker に委ねる。
5. **検証方法**: page.tsx はクライアントコンポーネントで既存 unit test
   (`tests/*.test.ts` = node:test + tsx、純関数対象) の射程外。
   `npm run lint` (= tsc --noEmit) 通過と文言の目視確認が現実的な確認線
   (node_modules 未導入なら `npm ci` が先)。tsx は型を見ないので型を触ったら lint を必ず回す
   (P-0284 PROGRESS の罠)。

### ロールバック

revert PR 1 本で戻る。表示分岐の追加のみで、データ・manifest・RBAC には触れない。

## やらないこと

- **常駐組への transcript 表示そのものの追加** — spec 注記 P-9002 (台帳 P-9000 / P-0317) の論点。
  SSE (`/api/agents/[agentId]/events`)・transcript.ts・autopilot 側のセッション書き出しには
  触れない。1 PR 1 論点 (CHARTER §3)。
- **snapshot.ts / kubernetes.ts 等データ層の変更**。`transcriptAvailable: false` 固定は正なので動かさない。
- **AgentCard や role バッジ等、空状態以外の UI 変更**。
- **`ops/rules.json` / backlog.json / state.json 等 heart が直接 push する領域の更新**。

# P-9005 — 常駐エージェント選択時に「transcript 信号を待っています」と出る不整合を直す

## 目的

所有者の Telegram 明示依頼 (2026-08-24T20:22:40Z)。前回 dispatch P-9003
(dispatch_id=d-6c9f83c7bf3e4e81) は ox-alpha-free の混雑で失敗したため、モデル切替
(opencode-go/deepseek-v4-flash) 後の再送。verify は所有者の指示により省略。

ops-dashboard のエージェントライブで常駐エージェント (autopilot-core / autopilot-heart) を
選ぶと、transcript が来る見込みが設計上ないのに「transcript 信号を待っています /
ファイルが作られると自動で表示します」と出る。待機ではないことが伝わる表示へ変える。

## 受入チェックリスト

spec の `verify` は空 (`[]`) — 所有者の指示により省略。機械検査コマンドが無いため、
受入基準は why/dod から派生して列挙する。**この仕様の要求は既に P-9003 の実装で満たされており、
本ブランチ (`project/p-9005`) は main (= P-9003 merge 済み HEAD) と同一内容である。**

- [x] 常駐エージェント (autopilot-core / autopilot-heart) を選んだとき、scope__empty の
  強調文言が「transcript 信号を待っています」にならないこと。
  実測: `apps/ops-dashboard/app/src/app/page.tsx:224-231` に `agent?.resident` 分岐があり、
  resident 時は「常駐エージェントのため transcript 表示なし」を表示 (P-9003 の f3ce65450 で
  main に merge 済み、PR #607)。
- [x] 常駐エージェント選択時に「ファイルが作られる」ことを示唆する補足文言が出ないこと。
  実測: 同上の分岐で resident 時は「稼働状態はエージェントカードに表示されます」を表示。
  `apps/ops-dashboard/app/src/lib/snapshot.ts:57-69` は resident の transcriptAvailable を
  false に固定したまま (正しい事実表現、変更不要)。

## 設計方針

このブランチでの追加実装は不要。要求は P-9003 の実装 (f3ce65450) が満たす:

1. **変更点は page.tsx の空状態 1 箇所のみ** (既に実装済み)。`TranscriptViewer` 内の
   `scope__empty` に `agent?.resident` 分岐を追加し、resident / Job 由来 agent 有り /
   agent 無しの 3 分岐になっている。分岐様式は `AgentCard` (75, 83 行) の `agent.resident`
   利用と同じ。
2. **snapshot.ts は触らない**。resident の `transcriptAvailable: false` は正しい事実表現。
3. **並走プロジェクトへの配慮**。常駐にも transcript 表示を追加する別件 P-9004 が
   `origin/project/p-9004` にあるが、2026-08-24 時点で main 未達 (独自コミットは
   initializer 1 件のみ)。merge されたら「transcript 表示なし」が嘘になるため文言要再検討。

## ロールバック

このブランチのコード差分は無い。P-9003 由来の変更を戻す場合は revert 1 本で戻る。

## やらないこと

- **常駐組への transcript 表示そのものの追加** — P-9004 (台帳 P-9000 / P-0317) の論点。
- **snapshot.ts / kubernetes.ts 等データ層の変更**。
- **AgentCard や role バッジ等、空状態以外の UI 変更**。
- **`ops/rules.json` / backlog.json / state.json 等 heart が直接 push する領域の更新**。
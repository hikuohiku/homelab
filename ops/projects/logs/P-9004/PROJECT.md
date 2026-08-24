# P-9004 — ダッシュボードのエージェントライブに core / heart の transcript 表示を追加

## 目的

所有者の Telegram 明示依頼 (2026-08-24T20:22:40Z)。前回 dispatch P-9002
(dispatch_id=d-4532c8ad284ce4a8) は ox-alpha-free の混雑で失敗したため、モデル切替
(opencode-go/deepseek-v4-flash) 後の再送。verify は所有者の指示により省略。

ops-dashboard のエージェントライブで、常駐エージェント (autopilot-core / autopilot-heart) の
transcript を見えるようにする。現行は resident の `transcriptAvailable` を false 固定
(`apps/ops-dashboard/app/src/lib/snapshot.ts:57-69`)、`parseAgentName` は Job 由来の役割名
(runner|reviewer|curriculum|critic|consolidation|chore)-<projectId>-aN しか解釈せず
(`apps/ops-dashboard/app/src/lib/transcript.ts:172-177`)、`/data/transcripts/` 配下に
core/heart の出力がどこにも落ちていない。

## 受入チェックリスト

spec の `verify` は空 (`[]`) — 所有者の指示により省略された (2026-08-24 の dispatch verify 廃止、
PR #604/#606、P-9003 と同じ経緯)。機械検査コマンドが無いため、受入基準は why/dod から派生して
列挙する。initializer が 2026-08-24 に `project/p-9004` checkout のコード実読で現状を確認した結果、
**下記 5 項目とも現時点で failing**。

- [ ] 生産側: core (opencode セッション) と heart (beat ログ) の出力を PVC autopilot-data の
  `/data/transcripts/resident/<agent>.jsonl` (core / heart) へ JSONL 追記で落とす経路ができる。
  実測: リポジトリ全体の grep で `transcripts/resident` の参照ゼロ。core は
  `/data/shadow/curriculum.jsonl` に記録するだけ (`apps/autopilot-core/app/shadow.go:14,301`) で
  transcripts へは出力しない。heart は transcripts ディレクトリを metrics 集計・ローテーションで
  読むだけ (`ops/heart/heart.py:92,374-378`)。
- [ ] 表示側: `parseAgentName` が resident の agent id (autopilot-core / autopilot-heart) を解釈し、
  `findTranscriptFile` が `transcripts/resident/` を見る。
  実測: `transcript.ts:172-177` の正規表現は `(runner|…|chore)-<projectId>-a\d+` のみで、resident の
  id は Deployment 名そのもの (`kubernetes.ts:129` `id: String(d.metadata.name)`) なので解釈できず
  null → SSE `/api/agents/[agentId]/events` が 400 を返す (`route.ts:10`)。
- [ ] snapshot: resident の `transcriptAvailable` が実ファイルの存在から決まる。
  実測: `snapshot.ts:67` が false に固定 (コメント「常駐組は projectId / transcript を持たない」)。
- [ ] 常駐エージェントを選ぶと LIVE TRANSCRIPT パネルに transcript がライブ表示される。
  実測: `page.tsx:226-230` は P-9003 の「常駐エージェントのため transcript 表示なし」のまま
  (transcript は流れてこない)。
- [ ] `tests/resident-transcript.test.ts` が新設され、fixture からの resident 解釈・最新ファイル
  選択・イベント正規化をクラスタ外 unit test で検証する。
  実測: `apps/ops-dashboard/app/tests/` に core.test.ts / snapshot.test.ts / transcript.test.ts のみで
  該当テストは存在しない。

## 設計方針

前提は initializer が 2026-08-24 に実読済み。調べ直さなくてよい。

1. **同一趣向の P-0317 が採択済み** (archive.jsonl:319、2026-08-24T18:43Z、人間依頼、`adopted: true`)。
   spec が明記する通り統合・破棄の判断は heart に委ねる。P-9004 は同題の P-9000 (dispatch 系譜:
   P-9002 → 再送) に当たるため、worker は着手前に P-0317 の並走状況を確認し、採択済みなら
   P-0317 の DoD / verify を借用して一本化するのが望ましい (同一作業の二重実施は CHARTER の無駄)。
2. **P-0317 の DoD が実装の形を与える**: (1) 生産側は core/heart の出力を
   `/data/transcripts/resident/<agent>.jsonl` へ JSONL 追記で tee (Job ランナの tee と同型
   `ops/runner/runner.py:814-824`、ローテーション込み)。(2) 表示側は transcript.ts に resident
   モード追加 (`parseAgentName` が core/heart を解釈、`findTranscriptFile` が
   `transcripts/resident/` を見る)。snapshot.ts の resident は `transcriptAvailable` を実ファイル
   存在から出す。(3) 既存ビューア (`normalizeTranscriptEvent` 経由) で表示できるイベント形式で吐く。
3. **ローテーションは既存が拾う**: `ops/heart/metrics.py rotate_transcripts` は
   `transcripts/` を rglob で回すので、resident/ も自動で保持期間・サイズ上限の対象になる
   (`heart.py:652`)。production 側はサイズを意識した tee に留め、回転ロジックは新設不要。
4. **deployment の env 実体**: `apps/ops-dashboard/deployment.yaml:37-38` は
   `TRANSCRIPTS_DIR=/data/transcripts` を設定するが、`transcript.ts:8` は `HEART_DATA_DIR ?? "/data"`
   を読む (TRANSCRIPTS_DIR は現状未使用)。transcript 読み出しは `/data/transcripts/<role>/` で、
   role は `transcriptMode()` (reviewer→review) が作る。resident 対応ではここに
   `resident` ディレクトリが加わる。
5. **P-9003 との関係**: P-9003 は resident 選択時の空状態を「常駐エージェントのため transcript
   表示なし」に変えた (page.tsx:226-230)。本件が入ると常駐にも transcript が流れるため、この文言は
   再び「実際の transcript」に置き換わる (P-9003 PROGRESS も P-9002 系 merge 時に再検討と明記)。
6. **検証方法**: dashboard は `npx tsx --test tests/*.test.ts` (クラスタ外 unit test) が既存線。
   `npm run lint` (= tsc --noEmit) も型を触ったら必ず回す (node_modules 未導入なら `npm ci` が先。
   tsx は型を見ない — P-0284 PROGRESS の罠)。SSE 実動作はクラスタ内スモーク
   (`ops/tools/dashboard_smoke.py`) が既存。

### ロールバック

revert PR 1 本で戻る。transcript 書き出し (production) と表示 (dashboard) の追加のみで、
データ破壊・RBAC・rules.json には触れない。resident/ 配下の jsonl は retention で自然消滅する。

## やらないこと

- **P-0317 との統合・破棄の判断** — heart の管轄。worker は並走を検知したら PROGRESS に記録して
  heart に委ねる。同一作業の二重 PR は作らない。
- **Job 由来 agent (runner|reviewer|curriculum|critic|consolidation|chore) の transcript 表示の変更** —
  既存機能は現状維持。resident 専用の経路を足すだけ。
- **backup / データ移行 / PVC・ストレージ変更等のインフラ操作**。
- **`ops/rules.json` / backlog.json / state.json 等 heart が直接 push する領域の更新**。
- **heart の metrics 集計・ローテーションロジックの変更** — 既存 `rotate_transcripts` が resident/
  も対象にするため、実測で足りない場合のみ触る (過剰設計を避ける)。
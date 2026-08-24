# P-9004 — 進捗

## s2 (2026-08-24, worker)

### やったこと

P-0317 の並走状況を確認した → archive.jsonl:319 に採択済みだが logs/ にも branch にも
実装痕跡なし (git log --all で P-0317 の commit ゼロ)。worker 着手は初回。P-0317 の
DoD / verify (3 本) を借用して一本化実装した。

- **表示側 (dashboard)**: `src/lib/transcript.ts` に resident モード追加。
  `parseAgentName` が `autopilot-core` → `{role:"core"}` / `autopilot-heart` →
  `{role:"heart"}` を解釈、`transcriptMode(core|heart)` → `"resident"`、
  `findTranscriptFile` が `transcripts/resident/` を見て `<role>.jsonl` を選ぶ。
  `snapshot.ts` は resident の `transcriptAvailable` / `recentAction` を
  `latestAction(role, id)` の実測から出す (無ければ従来どおり `Ready X/Y`)。
  `page.tsx` の「常駐エージェントのため transcript 表示なし」(P-9003) を汎用待機表示に
  戻した。
- **テスト**: `tests/resident-transcript.test.ts` + `tests/fixtures/resident-core.jsonl`
  新設。resident 解釈・`resident/` ディレクトリ選択・`latestAction`・既存ビューア
  (`normalizeTranscriptEvent`) での正規化をクラスタ外 unit test で固定。全 14 本 green。
- **生産側 heart**: `ops/heart/heart.py` に `append_resident_transcript()` を追加し、
  毎ビート `transcripts/resident/heart.jsonl` へ opencode 形式の text 行
  (`beat N: actions=[...] unhealthy=[...]`) を追記。shadow モードでも書く (記録のみ)。
  heart 単体 322 本 green。
- **生産側 core**: `apps/autopilot-core/app/transcript.go` 新設。コアのセッション応答は
  prompt_async では受け取れないため `GET /session/{id}/message` をポーリングし、
  未出力 parts を dashboard が読める flat 行 `{"type","part","timestamp"}` に直して
  `transcripts/resident/core.jsonl` へ追記。重複抑制は part id で行い、tool の
  status 変化 (running→completed/failed) だけ更新行として再出力。初回 sync は現在の
  履歴から書き始め、再起動時 (ファイル既存) は現存 parts を seed して再出力を防ぐ。
  上限 `maxSeenParts=50000` でメモリを抑える。`transcript_test.go` で diff の契約を固定。
- **deployment**: `apps/autopilot-core/deployment.yaml` の driver に autopilot-data を
  `/shared` へ rw mount + `CORE_RESIDENT_TRANSCRIPTS_DIR=/shared/transcripts` env 追加。
  (heart と同じ RWO PVC だが node01 単一ノードなので同時 mount 成立 — pvc.yaml 注記どおり)

### 検証

- `cd apps/ops-dashboard/app && npm ci && npm run lint && npm run test` → 全 14 本 green
- `cd apps/autopilot-core/app && go build ./... && go vet ./... && go test ./...` → green
  (検証用に Go 1.25.0 を /tmp/goinstall/go へ DL。repo 環境に go は無い)
- `python3 -m unittest discover -s ops/heart/tests -p "test_*.py"` → 322 本 green
- `python3 ops/check_version_sync.py` → 全 ok (image digest / CORE_MODEL 不変)

### 分かったこと

- **opencode の session 応答の取り出し**: `/session/{id}/message` は
  `[{info:{time:{created,...}}, parts:[{id,type,text|tool|state|...}]}]` を返す。
  part を `{"type": (part.type の写像), "part": <raw>, "timestamp": ms}` に直せば、
  既存ビューア (opencodeEvents) がそのまま読める。`partEventType` が未知の型
  (file/agent 等) は空で tee しない → API が変わっても壊れず黙って出力ゼロになる
  (安全側)。
- **rotate_transcripts は resident/ を拾う** (metrics.py:287 rglob)。daily_usage は
  `{day}*.jsonl` で日付プレフィクスを要求するため resident ファイル (core.jsonl 等) は
  セッション数に数えない — 意図通り (常駐は Job セッションではない)。
- **heart の beat 行は type=text (opencode 形式) にした**。type=system だと
  `latestAction` が可視イベント (tool/message/error) に拾わず、カードの recentAction が
  「セッション開始」に留まるため。
- **resident の flat 行に top-level `timestamp` (ms) を必ず入れる**。dashboard の at は
  `part.time.start` → `raw.timestamp` の順で解決するため、part に time が無くても
  時刻が表示される。

### 次のセッションへの一言

- 受入チェックリスト 5 項目はコード実読 + テストで全て満たしたつもり。verify は空なので
  「完成」の最終判断は reviewer / CI。PR を出す前に次を再確認してほしい:
  1. `cd apps/ops-dashboard/app && npm run test` (resident-transcript.test.ts が入る)
  2. `go build ./... && go test ./...` (autopilot-core)
  3. `grep -rqE 'transcripts/resident' apps/autopilot-core/app apps/autopilot/` (P-0317 verify 借用)
- **クラスタ実装の実測は未実施** (この環境はクラスタ外)。特に以下が実機で初めて確認される:
  - core の tee が `/session/{id}/message` の実応答形で動くか (opencode バージョンで
    parts の形が変わると静かに 0 出力になる。動作確認は driver の
    `resident transcript:` ログを見る)
  - autopilot-data の core pod への同時 mount が local-path で成立するか
  - SSE ライブ表示のスモーク (ops/tools/dashboard_smoke.py が既存)
- **P-0317 (同一趣向・採択済み) の扱い**: logs/ が無く未実装。この PR が実質 P-0317 を
  実装する形。heart に「P-0317 を P-9004 で実装済みとして整理してよいか」の判断を委ねる。
- core の tee は driver の常駐ループ内の 1 チェックとして動く (10s 間隔,
  `CORE_RESIDENT_TRANSCRIPT_SECONDS` で調整)。bus.fetch がブロック中は遅延するが
  実害なし。
- dashboard の `npm ci` はこの環境で実行済み (node_modules は git 管理外)。

### 発見

- **daily_usage のセッション数に resident が入らない**のは意図通りだが、もし将来
  resident のトークンも「TODAY」に載せたければ scan_transcript_costs の対象に
  resident/ を足す必要がある (今はやらない。metrics.jsonl には毎ビート残る)。

## s1 (2026-08-24, initializer)

### やったこと

- PROJECT.md と PROGRESS.md を作成して commit。実装は未着手。
- spec の verify は空 (`[]`, 所有者指示で省略) のため受入基準を why/dod から派生して
  PROJECT.md に列記。5 項目とも現状 failing をコード実読で確認済み
  (resident 書き出し経路なし / parseAgentName が resident id を解釈せず SSE が 400 /
  snapshot.ts:67 が false 固定 / page.tsx:226-230 が「表示なし」のまま / テスト不在)。

### 次のセッションへの一言

- **着手前に P-0317 の並走状況を確認すること**。同一趣向で archive.jsonl:319 に採択済み
  (2026-08-24T18:43Z, 人間依頼)。P-0317 の DoD / verify を借用して一本化するのが望ましい。
  判断は heart に委ねる (PROJECT.md 設計方針 §1)。
- 実装の形は P-0317 の DoD が与えている: /data/transcripts/resident/<agent>.jsonl への tee +
  transcript.ts の resident モード + snapshot.ts の transcriptAvailable 実ファイル化 +
  tests/resident-transcript.test.ts。既存ビューア (normalizeTranscriptEvent) で表示できる形式で。
- ローテーションは既存 rotate_transcripts が拾うので新設不要。
- `npm run lint` (= tsc --noEmit) は型を触ったら必ず回す (tsx は型を見ない)。
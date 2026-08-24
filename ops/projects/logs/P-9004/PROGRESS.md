# P-9004 — 進捗

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
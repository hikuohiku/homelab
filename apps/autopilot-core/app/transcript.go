// 常駐コアの transcript を transcripts/resident/core.jsonl へ tee する (P-9004)。
//
// コアの会話は opencode serve の常駐セッションに保存され、この driver は
// prompt_async で話しかけるだけで応答を直接受け取らない。そこで
// GET /session/{id}/message をポーリングし、まだ出力していない parts を
// dashboard の normalizeTranscriptEvent が読める flat イベント行
// {"type": ..., "part": ..., "timestamp": ...} に直して追記する。
//
// Job ランナの tee (runner.py Session) と違い、セッションはサーバ側に保存される
// ため「stdout を横取り」できない。取得は API 経由になるが、書くものは同じ
// JSONL 追記で、ローテーションも既存の rotate_transcripts が transcripts/ を
// rglob するので新設しない。
//
// 再起動・セッション張り直しで履歴を再出力しないよう、初回 sync で現存
// parts を「出力済み」として seed する。重複抑制は part id で行い、tool だけは
// status (running → completed/failed) の変化を更新行として再出力する —
// dashboard の mergeTranscriptEvent が id で合成するため、重複しても正しく
// 表示される (実行中が「完了」に置き換わる)。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// maxSeenParts は part id の重複抑制テーブルの上限。常駐セッションは長生き
// するため無制限に持つとメモリが膨らむ。超えたら古い id から順に捨て、その
// parts は再出力されるが (非 tool は 1 行重複するだけ)、稀で自己回復する
const maxSeenParts = 50000

// residentTranscript は常駐コアのセッション出力を flat イベント行にして
// outputPath へ追記するポーラー。
type residentTranscript struct {
	client     *client
	sessionID  string
	outputPath string
	seen       map[string]string // partID -> tool なら最後に出力した status、非 tool は ""
	order      []string          // seen の FIFO (上限で古い順に捨てる)
	seeded     bool
}

func newResidentTranscript(c *client, sessionID, outputPath string) *residentTranscript {
	return &residentTranscript{
		client:     c,
		sessionID:  sessionID,
		outputPath: outputPath,
		seen:       map[string]string{},
	}
}

// residentTranscriptInterval はポーリング間隔。コアは応答中でなければ parts が
// 増えないので、数秒に 1 回でライブ表示には十分 (ライブ表示の遅延の上限になる)
func residentTranscriptInterval() time.Duration {
	return time.Duration(envOrInt("CORE_RESIDENT_TRANSCRIPT_SECONDS", 10)) * time.Second
}

// opencodeMessage は GET /session/{id}/message の 1 要素。parts は中身の型が
// 多様なので raw のまま保持し、必要な分だけ個別に解釈する。
type opencodeMessage struct {
	Info struct {
		Time struct {
			Created int64 `json:"created"`
		} `json:"time"`
	} `json:"info"`
	Parts []json.RawMessage `json:"parts"`
}

// opencodePartMeta は part のうち tee に必要な最小限のフィールド。
type opencodePartMeta struct {
	ID    string         `json:"id"`
	Type  string         `json:"type"`
	State map[string]any `json:"state"`
}

// partEventType は opencode の part.type を dashboard の flat イベントの type へ
// 写す。dashboard が読めない型 (file / agent など) は空で tee しない。
func partEventType(partType string) string {
	switch partType {
	case "text", "reasoning", "tool":
		return partType
	case "step-start":
		return "step_start"
	case "step-finish":
		return "step_finish"
	}
	return ""
}

// partStatus は tool の状態を取り出す。非 tool は空。
func partStatus(state map[string]any) string {
	if state == nil {
		return ""
	}
	s, _ := state["status"].(string)
	return s
}

// eventLine は 1 parts を dashboard が読める flat 行にする。timestamp は
// セッション側の時刻 (ms) で、part 自身に time があれば dashboard がそちらを
// 優先する (transcript.ts の at 解決順)。
func eventLine(typ string, part json.RawMessage, ts int64) json.RawMessage {
	line, err := json.Marshal(map[string]any{
		"type":      typ,
		"part":      json.RawMessage(part),
		"timestamp": ts,
	})
	if err != nil {
		// 呼び元で常に valid な JSON を渡すため、ここが失敗することはない
		return json.RawMessage(`{"type":"text","part":{"type":"text","text":"(tee error)"}}`)
	}
	return line
}

// seed は再起動・セッション張り直し時の再出力を防ぐため、現存 parts を
// 出力済み扱いで埋める。上限を超える場合は古い方から捨てて新しい方を残す。
func (rt *residentTranscript) seed(messages []opencodeMessage) {
	for _, m := range messages {
		for _, rawPart := range m.Parts {
			var meta opencodePartMeta
			if json.Unmarshal(rawPart, &meta) != nil || meta.ID == "" {
				continue
			}
			if _, ok := rt.seen[meta.ID]; ok {
				continue
			}
			if len(rt.seen) >= maxSeenParts {
				oldest := rt.order[0]
				rt.order = rt.order[1:]
				delete(rt.seen, oldest)
			}
			rt.seen[meta.ID] = partStatus(meta.State)
			rt.order = append(rt.order, meta.ID)
		}
	}
}

// diff はまだ出力していない parts と、tool の status 変化を flat 行にして返す。
// 同時に seen を更新する (I/O はしない。テストが直接叩く)。
func (rt *residentTranscript) diff(messages []opencodeMessage) []json.RawMessage {
	var out []json.RawMessage
	for _, m := range messages {
		ts := m.Info.Time.Created
		if ts == 0 {
			ts = time.Now().UnixMilli()
		}
		for _, rawPart := range m.Parts {
			var meta opencodePartMeta
			if json.Unmarshal(rawPart, &meta) != nil {
				continue
			}
			typ := partEventType(meta.Type)
			if typ == "" {
				continue
			}
			status := partStatus(meta.State)
			prev, known := rt.seen[meta.ID]
			if !known {
				out = append(out, eventLine(typ, rawPart, ts))
				rt.seen[meta.ID] = status
				rt.order = append(rt.order, meta.ID)
				continue
			}
			// tool は status の変化を更新行として再出力する (実行中 → 完了)。
			// 非 tool は同一 id の再出力が無い (parts は append 専用で変わらない)
			if typ == "tool" && status != "" && status != prev {
				out = append(out, eventLine(typ, rawPart, ts))
				rt.seen[meta.ID] = status
			}
		}
	}
	rt.trim()
	return out
}

// trim は上限を超えた分を古い id から捨てる。
func (rt *residentTranscript) trim() {
	for len(rt.seen) > maxSeenParts {
		oldest := rt.order[0]
		rt.order = rt.order[1:]
		delete(rt.seen, oldest)
	}
}

// sync は 1 回のポーリング分: 取得 → (初回は必要なら seed) → diff → 追記。
func (rt *residentTranscript) sync(ctx context.Context) error {
	messages, err := rt.fetch(ctx)
	if err != nil {
		return err
	}
	if !rt.seeded {
		// 既に出力がある (再起動・セッション張り直し) ときだけ現存 parts を
		// 出力済み扱いで seed する。初回 (空ファイル) は履歴から書き始める
		if rt.hasContent() {
			rt.seed(messages)
			log.Printf("resident transcript: 既存 %d parts を出力済み扱いで seed した", len(rt.seen))
		}
		rt.seeded = true
	}
	lines := rt.diff(messages)
	if len(lines) == 0 {
		return nil
	}
	if err := rt.append(lines); err != nil {
		return err
	}
	return nil
}

// hasContent は出力ファイルに既に 1 行でも書かれているかを返す。
func (rt *residentTranscript) hasContent() bool {
	info, err := os.Stat(rt.outputPath)
	return err == nil && info.Size() > 0
}

// fetch は GET /session/{id}/message を叩いて parts 列を返す。limit を付けて
// 直近だけを見る (古い parts は出力済みで、status も変動しない)。
func (rt *residentTranscript) fetch(ctx context.Context) ([]opencodeMessage, error) {
	status, raw, err := rt.client.opencode(ctx, http.MethodGet,
		"/session/"+rt.sessionID+"/message?limit=2000", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("session message を読めない (status=%d)", status)
	}
	var messages []opencodeMessage
	if err := json.Unmarshal(raw, &messages); err != nil {
		return nil, fmt.Errorf("session message が JSON として壊れている: %w", err)
	}
	return messages, nil
}

// append は flat 行を outputPath に追記する (JSONL)。
func (rt *residentTranscript) append(lines []json.RawMessage) error {
	if dir := filepath.Dir(rt.outputPath); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}
	f, err := os.OpenFile(rt.outputPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	for _, line := range lines {
		if _, err := f.Write(append(line, '\n')); err != nil {
			return err
		}
	}
	return nil
}
// 常駐コアの transcript tee の契約を固定する。
//
// 守りたいこと:
//   - part を flat イベント行 (dashboard の normalizeTranscriptEvent が読める形) に写す
//   - 同じ part を再出力しない (tool の status 変化は更新行として再出力する)
//   - 再起動・セッション張り直しで既存のファイルへ履歴を再出力しない
//   - 重複抑制テーブルは上限を超えて肥大しない
//
//	cd apps/autopilot-core/app && go test ./...
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// coreMessages は GET /session/{id}/message の典型的な応答。opencode の flat 行
// (fixtures/resident-core.jsonl) の元になる形。
const coreMessages = `[
  {"info":{"id":"msg_1","role":"user","time":{"created":1787384162000,"completed":1787384162100}},
   "parts":[
     {"id":"prt_text1","type":"text","text":"書き置き"},
     {"id":"prt_reason1","type":"reasoning","text":"確認する"}
   ]},
  {"info":{"id":"msg_2","role":"assistant","time":{"created":1787384163000,"completed":1787384163100}},
   "parts":[
     {"id":"prt_start","type":"step-start"},
     {"id":"prt_tool1","type":"tool","callID":"call_1","tool":"bash","state":{"status":"running","input":{"command":"git log"}}},
     {"id":"prt_finish","type":"step-finish","cost":0.01,"tokens":{"input":100,"output":20}}
   ]}
]`

func parseMessages(t *testing.T, raw string) []opencodeMessage {
	t.Helper()
	var messages []opencodeMessage
	if err := json.Unmarshal([]byte(raw), &messages); err != nil {
		t.Fatalf("fixture が JSON として壊れている: %v", err)
	}
	return messages
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("読み取れない %s: %v", path, err)
	}
	return string(raw)
}

func TestPartEventTypeMapsToDashboardTypes(t *testing.T) {
	cases := map[string]string{
		"text": "text", "reasoning": "reasoning", "tool": "tool",
		"step-start": "step_start", "step-finish": "step_finish",
		// dashboard が読めない型は tee しない
		"file": "", "agent": "", "snapshot": "",
	}
	for in, want := range cases {
		if got := partEventType(in); got != want {
			t.Errorf("partEventType(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestResidentDiffEmitsNewPartsAndToolUpdates(t *testing.T) {
	rt := newResidentTranscript(nil, "", "")
	lines := rt.diff(parseMessages(t, coreMessages))
	if len(lines) != 5 {
		t.Fatalf("初回は 5 parts を出力するはず: got %d", len(lines))
	}
	if again := rt.diff(parseMessages(t, coreMessages)); len(again) != 0 {
		t.Fatalf("同じ内容なら再出力しないはず: got %d", len(again))
	}
	// tool が running → completed に変わったら更新行だけを出す
	updated := strings.Replace(coreMessages, `"status":"running"`, `"status":"completed"`, 1)
	if lines := rt.diff(parseMessages(t, updated)); len(lines) != 1 {
		t.Fatalf("tool の status 変化は 1 行だけ出すはず: got %d", len(lines))
	}
	if again := rt.diff(parseMessages(t, updated)); len(again) != 0 {
		t.Fatalf("更新後の同一内容は再出力しないはず: got %d", len(again))
	}
}

func TestEventLineIsFlatDashboardForm(t *testing.T) {
	rt := newResidentTranscript(nil, "", "")
	lines := rt.diff(parseMessages(t, coreMessages))
	if len(lines) != 5 {
		t.Fatalf("予期しない行数: %d", len(lines))
	}
	var step struct {
		Type      string          `json:"type"`
		Part      json.RawMessage `json:"part"`
		Timestamp int64           `json:"timestamp"`
	}
	if err := json.Unmarshal(lines[2], &step); err != nil {
		t.Fatalf("flat 行が JSON として壊れている: %v", err)
	}
	if step.Type != "step_start" || step.Timestamp == 0 {
		t.Fatalf("flat 行の形が変: %s", lines[2])
	}
	var tool struct {
		Type string `json:"type"`
		Part struct {
			ID   string `json:"id"`
			Type string `json:"type"`
		} `json:"part"`
	}
	if err := json.Unmarshal(lines[3], &tool); err != nil {
		t.Fatal(err)
	}
	if tool.Type != "tool" || tool.Part.ID != "prt_tool1" || tool.Part.Type != "tool" {
		t.Fatalf("tool 行の形が変: %s", lines[3])
	}
}

func TestResidentDiffIsBounded(t *testing.T) {
	rt := newResidentTranscript(nil, "", "")
	var messages []opencodeMessage
	for i := 0; i < maxSeenParts+5; i++ {
		part := json.RawMessage(fmt.Sprintf(`{"id":"prt_%d","type":"text","text":"x"}`, i))
		messages = append(messages, opencodeMessage{Parts: []json.RawMessage{part}})
	}
	rt.diff(messages)
	if len(rt.seen) > maxSeenParts {
		t.Fatalf("上限を超えて保持しないはず: %d", len(rt.seen))
	}
	// trim で捨てた古い分だけが再出力される
	if lines := rt.diff(messages); len(lines) > 5 {
		t.Fatalf("trim 後はせいぜい数行の再出力のはず: %d", len(lines))
	}
}

func TestResidentTranscriptSyncFirstRunWritesHistory(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "resident", "core.jsonl")
	state := coreMessages
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Path, "/session/ses_x/message") {
			t.Fatalf("session message を見るべき: %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(state))
	}))
	defer server.Close()

	c := newClient(&config{opencodeURL: server.URL, stateDir: dir})
	rt := newResidentTranscript(c, "ses_x", out)
	ctx := context.Background()

	// 初回 (空ファイル) は現在の履歴をそのまま書き始める
	if err := rt.sync(ctx); err != nil {
		t.Fatalf("初回 sync: %v", err)
	}
	if got := len(strings.Split(strings.TrimSpace(readFile(t, out)), "\n")); got != 5 {
		t.Fatalf("初回は 5 行書くはず: got %d", got)
	}
	// 同一内容なら追記しない
	before := readFile(t, out)
	if err := rt.sync(ctx); err != nil {
		t.Fatalf("2 回目 sync: %v", err)
	}
	if readFile(t, out) != before {
		t.Fatal("同一内容なら追記しないはず")
	}

	// tool の status 変化は更新行として追記される
	state = strings.Replace(coreMessages, `"status":"running"`, `"status":"completed"`, 1)
	if err := rt.sync(ctx); err != nil {
		t.Fatalf("tool 更新 sync: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(readFile(t, out)), "\n")
	if len(lines) != 6 {
		t.Fatalf("tool 更新で 1 行追記されるはず: got %d", len(lines))
	}
	var last struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &last); err != nil {
		t.Fatal(err)
	}
	if last.Type != "tool" {
		t.Fatalf("最後の行は tool 更新のはず: %q", lines[len(lines)-1])
	}
}

func TestResidentTranscriptSyncSeedsOnRestart(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "resident", "core.jsonl")
	// 前回起動の成果物が既にある
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(out, []byte("x\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(coreMessages))
	}))
	defer server.Close()

	c := newClient(&config{opencodeURL: server.URL, stateDir: dir})
	rt := newResidentTranscript(c, "ses_x", out)
	ctx := context.Background()
	if err := rt.sync(ctx); err != nil {
		t.Fatalf("再起動後の sync: %v", err)
	}
	if readFile(t, out) != "x\n" {
		t.Fatalf("再起動時は既存履歴を再出力しないはず: %q", readFile(t, out))
	}
}
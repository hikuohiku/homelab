// このサイドカーが守るべきことは 3 つしかない。
//
//  1. heart が既読の鍵にできる名前 ("<id>.json") でしか置かない
//  2. 書き終わってから ack する (逆順だと落ちたときにイベントが消える)
//  3. 書けなかったら ack しない (再配送で拾い直す)
//
// 実サーバ無しで確かめられるよう、busMessage を差し替えて検証する。
package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeMessage は ack/term の呼ばれ方と、その時点でファイルが在ったかを記録する。
type fakeMessage struct {
	payload []byte
	dir     string
	name    string

	acked      int
	termed     int
	ackErr     error
	ackOrderOK bool
}

func (m *fakeMessage) data() []byte { return m.payload }

func (m *fakeMessage) ack() error {
	m.acked++
	if m.name != "" {
		_, err := os.Stat(filepath.Join(m.dir, m.name))
		m.ackOrderOK = err == nil
	}
	return m.ackErr
}

func (m *fakeMessage) term() error { m.termed++; return nil }

func msgFor(t *testing.T, dir string, e busEvent) *fakeMessage {
	t.Helper()
	raw, err := json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	return &fakeMessage{payload: raw, dir: dir, name: e.ID + ".json"}
}

func testConfig(dir string) config {
	return config{
		outDir: dir, commandDir: filepath.Join(dir, "commands"),
		pollSeconds: 1, retain: time.Hour, fetchMax: 16,
	}
}

func commandMsg(t *testing.T, cfg config, c commandEvent) *fakeMessage {
	t.Helper()
	raw, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	return &fakeMessage{payload: raw, dir: cfg.commandDir, name: c.CommandID + ".json"}
}

func commandConfig(t *testing.T) config {
	t.Helper()
	cfg := testConfig(t.TempDir())
	if err := os.MkdirAll(cfg.commandDir, 0o755); err != nil {
		t.Fatal(err)
	}
	return cfg
}

func sampleCommand() commandEvent {
	return commandEvent{
		CommandID: "core-0123456789abcdef",
		Type:      "task-request",
		Source:    "core",
		IssuedAt:  "2026-08-23T12:00:00Z",
		Title:     "nats の掃除",
		Body:      "ストリームが太っている",
	}
}

func TestConsumeWritesNoteThenAcks(t *testing.T) {
	// ack は「書き終えた後」でなければならない。ack が先だと、その直後に落ちた
	// ときイベントが誰にも渡らないまま消える (誰も再送してくれない)
	dir := t.TempDir()
	m := msgFor(t, dir, busEvent{
		ID: "20260823-104000-01e240", Source: "telegram",
		Received: "2026-08-23T10:40:00Z", Body: "止めて",
	})
	consume(testConfig(dir), []busMessage{m})

	if m.acked != 1 || m.termed != 0 {
		t.Fatalf("ack 1 / term 0 のはず: acked=%d termed=%d", m.acked, m.termed)
	}
	if !m.ackOrderOK {
		t.Fatal("ack した時点でファイルが無い (書く前に ack している)")
	}
	raw, err := os.ReadFile(filepath.Join(dir, "20260823-104000-01e240.json"))
	if err != nil {
		t.Fatalf("note が置かれていない: %v", err)
	}
	var got busEvent
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("置いた note が JSON として読めない: %v", err)
	}
	if got.Body != "止めて" || got.Source != "telegram" {
		t.Fatalf("中身が保たれていない: %+v", got)
	}
}

func TestConsumeFileNameIsHeartSeenKey(t *testing.T) {
	// heart の cursor は GitHub の inbox ファイル名 ("<id>.json") で既読を持つ。
	// ここが 1 文字でもずれると 2 経路の重複を落とせず、同じ「止めて」で
	// 2 回停止処理が走る
	dir := t.TempDir()
	m := msgFor(t, dir, busEvent{ID: "note-1", Body: "やめて"})
	consume(testConfig(dir), []busMessage{m})

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "note-1.json" {
		t.Fatalf("ファイル名が inbox と揃っていない: %v", entries)
	}
}

func TestConsumeIsIdempotent(t *testing.T) {
	// 再配送 (ack が届かなかったときに起きる) で同じイベントが 2 回来ても、
	// 置かれるファイルは 1 つ。heart 側から見て新着が 2 回発生しない
	dir := t.TempDir()
	e := busEvent{ID: "note-1", Body: "最初の本文"}
	consume(testConfig(dir), []busMessage{msgFor(t, dir, e)})

	e2 := e
	e2.Body = "後から来た別の本文"
	consume(testConfig(dir), []busMessage{msgFor(t, dir, e2)})

	raw, err := os.ReadFile(filepath.Join(dir, "note-1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var got busEvent
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got.Body != "最初の本文" {
		t.Fatalf("既存の note を上書きしている: %q", got.Body)
	}
}

func TestConsumeDropsUnusableEvents(t *testing.T) {
	// 再配送されても直らないものは term する。term しないと AckWait ごとに
	// 永久に戻ってきてログを埋める
	dir := t.TempDir()
	cases := []struct {
		name string
		msg  *fakeMessage
	}{
		{"壊れた JSON", &fakeMessage{payload: []byte("{"), dir: dir}},
		{"id が無い", msgFor(t, dir, busEvent{Body: "止めて"})},
		{"本文が空", msgFor(t, dir, busEvent{ID: "note-1", Body: "   "})},
		{"パスとして危険な id", msgFor(t, dir, busEvent{ID: "../../etc/passwd", Body: "止めて"})},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			consume(testConfig(dir), []busMessage{c.msg})
			if c.msg.termed != 1 || c.msg.acked != 0 {
				t.Fatalf("term 1 / ack 0 のはず: acked=%d termed=%d", c.msg.acked, c.msg.termed)
			}
		})
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("捨てるべきイベントを書き出している: %v", entries)
	}
}

func TestConsumeDoesNotAckWhenWriteFails(t *testing.T) {
	// 書けなかったものを ack すると消える。ack しなければ AckWait 後に再配送される
	dir := filepath.Join(t.TempDir(), "存在しない")
	m := msgFor(t, dir, busEvent{ID: "note-1", Body: "止めて"})
	consume(testConfig(dir), []busMessage{m})
	if m.acked != 0 || m.termed != 0 {
		t.Fatalf("書けていないのに ack/term している: acked=%d termed=%d", m.acked, m.termed)
	}
}

func TestConsumeContinuesAfterAckFailure(t *testing.T) {
	// ack が失敗しても後続のイベントを捨てない (再配送は冪等に吸収される)
	dir := t.TempDir()
	bad := msgFor(t, dir, busEvent{ID: "note-1", Body: "一件目"})
	bad.ackErr = errors.New("ack できない")
	good := msgFor(t, dir, busEvent{ID: "note-2", Body: "二件目"})
	consume(testConfig(dir), []busMessage{bad, good})

	for _, name := range []string{"note-1.json", "note-2.json"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			t.Fatalf("%s が置かれていない: %v", name, err)
		}
	}
	if good.acked != 1 {
		t.Fatalf("後続が ack されていない: %d", good.acked)
	}
}

func TestWriteNoteLeavesNoPartialFile(t *testing.T) {
	// 一時ファイルは "." 始まりで、rename 後に残らない。heart の走査が
	// 書きかけの JSON を拾わないこと
	dir := t.TempDir()
	if _, err := writeNote(dir, busEvent{ID: "note-1", Body: "本文"}); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "note-1.json" {
		t.Fatalf("残骸がある: %v", entries)
	}
}

func TestPruneRemovesOldNotesOnly(t *testing.T) {
	// 掃除しないと共有ボリュームが単調に増える。新しいものは消さない
	dir := t.TempDir()
	old := filepath.Join(dir, "old.json")
	fresh := filepath.Join(dir, "fresh.json")
	for _, p := range []string{old, fresh} {
		if err := os.WriteFile(p, []byte("{}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	past := time.Now().Add(-48 * time.Hour)
	if err := os.Chtimes(old, past, past); err != nil {
		t.Fatal(err)
	}
	if n := prune(dir, 24*time.Hour, time.Now()); n != 1 {
		t.Fatalf("消した件数が 1 でない: %d", n)
	}
	if _, err := os.Stat(old); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("古い note が残っている")
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatal("新しい note を消している")
	}
}

func TestLoadConfigDefaultsMatchHeart(t *testing.T) {
	// 既定値が heart 側 (ops/heart/config.py の HEART_FEEDBACK_BUS_DIR) と
	// 揃っていること。ずれると「書いているのに読まれない」無言の故障になる
	t.Setenv("BUS_SIDECAR_OUT_DIR", "")
	cfg := loadConfig()
	if cfg.outDir != "/data/feedback-bus/inbox" {
		t.Fatalf("既定の書き出し先がずれている: %s", cfg.outDir)
	}
}

// --- コア発の command (events.heart.>) ---

func TestConsumeCommandsWritesToSeparateDirThenAcks(t *testing.T) {
	// 書き置きと同じディレクトリに混ぜると heart の triage が誤分類する。
	// ack は書き終えた後 (書き置き経路と同じ順序ルール)
	cfg := commandConfig(t)
	c := sampleCommand()
	m := commandMsg(t, cfg, c)

	consumeCommands(cfg, []busMessage{m})

	if m.acked != 1 || !m.ackOrderOK {
		t.Fatalf("書いてから ack すべき: acked=%d order=%v", m.acked, m.ackOrderOK)
	}
	raw, err := os.ReadFile(filepath.Join(cfg.commandDir, c.CommandID+".json"))
	if err != nil {
		t.Fatalf("command が落ちていない: %v", err)
	}
	var got commandEvent
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got.CommandID != c.CommandID || got.Body != c.Body || got.Type != c.Type {
		t.Fatalf("中身が変わっている: %+v", got)
	}
	entries, _ := os.ReadDir(cfg.outDir)
	for _, e := range entries {
		if !e.IsDir() {
			t.Fatalf("書き置き側に混ざっている: %s", e.Name())
		}
	}
}

func TestConsumeCommandsIsIdempotent(t *testing.T) {
	// 再配送されても 2 つプロジェクトが立ってはいけない。ファイル名が同じなら
	// 上書きしない (heart 側の台帳と二重の守り)
	cfg := commandConfig(t)
	c := sampleCommand()
	consumeCommands(cfg, []busMessage{commandMsg(t, cfg, c)})

	changed := c
	changed.Body = "後から来た別の本文"
	consumeCommands(cfg, []busMessage{commandMsg(t, cfg, changed)})

	raw, err := os.ReadFile(filepath.Join(cfg.commandDir, c.CommandID+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var got commandEvent
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got.Body != c.Body {
		t.Fatalf("同じ id を上書きしている: %q", got.Body)
	}
}

func TestConsumeCommandsDropsUnusable(t *testing.T) {
	cfg := commandConfig(t)
	broken := &fakeMessage{payload: []byte("{壊れた"), dir: cfg.commandDir}
	noID := commandMsg(t, cfg, commandEvent{Type: "task-request", Body: "本文"})
	badID := commandMsg(t, cfg, commandEvent{CommandID: "../逃げる", Type: "task-request", Body: "本文"})
	noType := commandMsg(t, cfg, commandEvent{CommandID: "core-1", Body: "本文"})
	noBody := commandMsg(t, cfg, commandEvent{CommandID: "core-2", Type: "task-request", Body: "  "})

	for _, m := range []*fakeMessage{broken, noID, badID, noType, noBody} {
		consumeCommands(cfg, []busMessage{m})
		if m.termed != 1 || m.acked != 0 {
			t.Fatalf("直らないものは term で落とす: termed=%d acked=%d", m.termed, m.acked)
		}
	}
	if entries, _ := os.ReadDir(cfg.commandDir); len(entries) != 0 {
		t.Fatalf("何も書かないはず: %v", entries)
	}
}

func TestConsumeCommandsDoesNotAckWhenWriteFails(t *testing.T) {
	// 書けないまま ack すると依頼が誰にも渡らずに消える
	cfg := testConfig(t.TempDir())
	cfg.commandDir = filepath.Join(cfg.outDir, "存在しない", "深い場所")
	m := commandMsg(t, cfg, sampleCommand())
	consumeCommands(cfg, []busMessage{m})
	if m.acked != 0 || m.termed != 0 {
		t.Fatalf("再配送に任せるべき: acked=%d termed=%d", m.acked, m.termed)
	}
}

func TestLoadConfigCommandDirDefaultMatchesHeart(t *testing.T) {
	// 既定値が heart 側 (ops/heart/config.py の HEART_COMMAND_BUS_DIR) と揃うこと
	t.Setenv("BUS_SIDECAR_COMMAND_DIR", "")
	cfg := loadConfig()
	if cfg.commandDir != "/data/command-bus/inbox" {
		t.Fatalf("既定の command 置き場がずれている: %s", cfg.commandDir)
	}
	if cfg.commandDir == cfg.outDir {
		t.Fatal("書き置きと同じ場所に落としてはいけない")
	}
}

// バス consume の契約を固定する。
//
// 守りたいのは 3 つ:
//   - 同じ書き置きが GitHub 経路と NATS 経路の両方から来ても、コアへ渡すのは 1 回だけ
//     (二重返信は過去に実際にやらかしている)
//   - ack はコアへ渡し切った後。先に ack すると落ちたときにイベントが消える
//   - NATS 未設定でも driver が動く (切り戻し構成)
//
// 実サーバ相手の統合テストは NATS_TEST_URL / NATS_TEST_SEED があるときだけ走る
// (telegram-adapter の bus_test.go と同じ流儀)。
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nkeys"
)

// fakeMessage は busMessage の偽物。ack/term の呼ばれ方と順序を記録する。
type fakeMessage struct {
	raw      []byte
	acked    int
	termed   int
	ackedAt  int // ack が呼ばれた時点で prompt が何回済んでいたか
	promptsN *int
}

func (m *fakeMessage) data() []byte { return m.raw }
func (m *fakeMessage) ack() error {
	m.acked++
	if m.promptsN != nil {
		m.ackedAt = *m.promptsN
	}
	return nil
}
func (m *fakeMessage) term() error { m.termed++; return nil }

func encodeEvent(t *testing.T, n note) []byte {
	t.Helper()
	raw, err := json.Marshal(n)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// promptRecorder は opencode の prompt_async を模した httptest サーバを返す。
func promptRecorder(t *testing.T, status int) (*httptest.Server, *[]string, *int) {
	t.Helper()
	var texts []string
	var count int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Parts []struct {
				Text string `json:"text"`
			} `json:"parts"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if len(body.Parts) > 0 {
			texts = append(texts, body.Parts[0].Text)
		}
		count++
		w.WriteHeader(status)
	}))
	t.Cleanup(srv.Close)
	return srv, &texts, &count
}

func TestSeenKeyUnifiesBothPaths(t *testing.T) {
	// NATS のイベント id と inbox のファイル名は同じ書き置きを指す。
	// 同じ鍵に落ちないと重複排除が効かない
	if seenKey("20260823-120317-1e88e232") != "20260823-120317-1e88e232.json" {
		t.Fatalf("id はファイル名の形に寄せるべき: %q", seenKey("20260823-120317-1e88e232"))
	}
	if seenKey("20260823-120317-1e88e232.json") != "20260823-120317-1e88e232.json" {
		t.Fatal("ファイル名はそのまま")
	}
	if seenKey("  ") != "" {
		t.Fatal("空は空 (鍵にできないものを鍵にしない)")
	}
}

func TestConsumeBusSkipsAlreadySeenFromGitHub(t *testing.T) {
	// GitHub 経路が先に拾った書き置きが NATS からも来る場合。
	// ここでコアへ渡すと同じ書き置きに 2 回返事をする
	srv, texts, _ := promptRecorder(t, http.StatusNoContent)
	c := newClient(&config{opencodeURL: srv.URL, stateDir: t.TempDir()})

	seen := map[string]bool{"20260823-120317-1e88e232.json": true}
	m := &fakeMessage{raw: encodeEvent(t, note{ID: "20260823-120317-1e88e232", Source: "telegram", Body: "止めて"})}

	if c.consumeBus(context.Background(), "ses_1", []busMessage{m}, seen,
		filepath.Join(t.TempDir(), "cursor.json")) {
		t.Fatal("既読を飛ばしただけでセッションを張り直す必要は無い")
	}
	if len(*texts) != 0 {
		t.Fatalf("既読はコアへ渡さない: %v", *texts)
	}
	if m.acked != 1 {
		t.Fatalf("既読でも ack して server 側に溜めない: acked=%d", m.acked)
	}
}

func TestConsumeBusPersistsSeenBeforeAck(t *testing.T) {
	// 再配送されたときに二度返事をしないこと。cursor は ack より先に永続化する
	srv, texts, _ := promptRecorder(t, http.StatusNoContent)
	dir := t.TempDir()
	cursorPath := filepath.Join(dir, "cursor.json")
	c := newClient(&config{opencodeURL: srv.URL, stateDir: dir})

	seen := map[string]bool{}
	m := &fakeMessage{raw: encodeEvent(t, note{ID: "20260823-120317-1e88e232", Source: "telegram", Body: "止めて"})}
	c.consumeBus(context.Background(), "ses_1", []busMessage{m}, seen, cursorPath)

	if len(*texts) != 1 {
		t.Fatalf("未読は 1 回だけ渡すべき: %v", *texts)
	}
	if !seen["20260823-120317-1e88e232.json"] {
		t.Fatal("渡したものは既読になっているべき (再配送で二度返事をしない)")
	}
	// cursor は ack の前に永続化されている。再起動しても既読が残ること
	reloaded, had := loadSeen(cursorPath)
	if !had || !reloaded["20260823-120317-1e88e232.json"] {
		t.Fatalf("cursor に残すべき: %v had=%v", reloaded, had)
	}
}

func TestConsumeBusAcksOnlyAfterPromptSucceeds(t *testing.T) {
	srv, _, count := promptRecorder(t, http.StatusNoContent)
	c := newClient(&config{opencodeURL: srv.URL, stateDir: t.TempDir()})

	m := &fakeMessage{raw: encodeEvent(t, note{ID: "a", Source: "telegram", Body: "本文"}), promptsN: count}
	c.consumeBus(context.Background(), "ses_1", []busMessage{m}, map[string]bool{},
		filepath.Join(t.TempDir(), "cursor.json"))

	if m.acked != 1 {
		t.Fatalf("渡せたら ack する: acked=%d", m.acked)
	}
	if m.ackedAt != 1 {
		t.Fatalf("ack は prompt の後 (prompt 済み %d 件の時点で ack された)", m.ackedAt)
	}
}

func TestConsumeBusDoesNotAckWhenPromptFails(t *testing.T) {
	// ack してしまうとイベントが消える。再配送に任せる
	srv, _, _ := promptRecorder(t, http.StatusInternalServerError)
	c := newClient(&config{opencodeURL: srv.URL, stateDir: t.TempDir()})

	m := &fakeMessage{raw: encodeEvent(t, note{ID: "a", Source: "telegram", Body: "本文"})}
	seen := map[string]bool{}
	if !c.consumeBus(context.Background(), "ses_1", []busMessage{m}, seen,
		filepath.Join(t.TempDir(), "cursor.json")) {
		t.Fatal("渡せなかったらセッションを張り直させる")
	}
	if m.acked != 0 || m.termed != 0 {
		t.Fatalf("失敗したら ack も term もしない: acked=%d termed=%d", m.acked, m.termed)
	}
	if len(seen) != 0 {
		t.Fatal("渡せていないものを既読にしてはいけない (以後どの経路でも拾われなくなる)")
	}
}

func TestConsumeBusDropsUnusableEvents(t *testing.T) {
	// 壊れた JSON と id 無しは再配送しても直らない。term で落とし、
	// 後続のイベントは普通に処理すること (1 件の毒で流れを止めない)
	srv, texts, _ := promptRecorder(t, http.StatusNoContent)
	c := newClient(&config{opencodeURL: srv.URL, stateDir: t.TempDir()})

	broken := &fakeMessage{raw: []byte("{not json")}
	noID := &fakeMessage{raw: encodeEvent(t, note{Source: "telegram", Body: "id が無い"})}
	ok := &fakeMessage{raw: encodeEvent(t, note{ID: "z", Source: "telegram", Body: "本文"})}

	c.consumeBus(context.Background(), "ses_1", []busMessage{broken, noID, ok}, map[string]bool{},
		filepath.Join(t.TempDir(), "cursor.json"))

	if broken.termed != 1 || broken.acked != 0 {
		t.Fatalf("壊れた JSON は term: termed=%d acked=%d", broken.termed, broken.acked)
	}
	if noID.termed != 1 {
		t.Fatalf("id が無いイベントは重複排除できないので term: termed=%d", noID.termed)
	}
	if len(*texts) != 1 {
		t.Fatalf("後続は普通に渡すべき: %v", *texts)
	}
}

func TestConsumeBusSkipsEmptyBody(t *testing.T) {
	// 本文が無いものでコアを起こさない (GitHub 経路と同じ扱い)
	srv, texts, _ := promptRecorder(t, http.StatusNoContent)
	c := newClient(&config{opencodeURL: srv.URL, stateDir: t.TempDir()})

	m := &fakeMessage{raw: encodeEvent(t, note{ID: "e", Source: "telegram", Body: "   "})}
	seen := map[string]bool{}
	c.consumeBus(context.Background(), "ses_1", []busMessage{m}, seen,
		filepath.Join(t.TempDir(), "cursor.json"))

	if len(*texts) != 0 {
		t.Fatalf("空の本文でコアを起こさない: %v", *texts)
	}
	if m.acked != 1 || !seen["e.json"] {
		t.Fatalf("既読にして ack する: acked=%d seen=%v", m.acked, seen)
	}
}

func TestConnectBusIsOptional(t *testing.T) {
	// NATS 未設定でも driver は動く。ここが error になると切り戻し構成で起動しなくなる
	t.Setenv("NATS_URL", "")
	t.Setenv("NATS_NKEY_SEED", "")

	bus, err := connectBus()
	if err != nil {
		t.Fatalf("未設定はエラーにしない: %v", err)
	}
	if bus != nil {
		t.Fatal("未設定なら nil を返すべき")
	}
	// nil に対して close を呼んでも落ちないこと (defer で無条件に呼ぶため)
	bus.close()
}

func TestConnectBusRejectsBrokenSeed(t *testing.T) {
	// 壊れた seed を黙って無視すると、バスを読まないまま「移行できたつもり」になる
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("NATS_NKEY_SEED", "not-a-seed")

	if _, err := connectBus(); err == nil {
		t.Fatal("壊れた seed は起動時に弾くべき")
	}
}

func TestConnectBusOrLogSurvivesUnreachableServer(t *testing.T) {
	// NATS が落ちていても driver は死なない。ここで落とすと、この driver が唯一の
	// 経路である所有者の「止めて」が、バスの不調ごと止まる (GitHub 経路は生きているのに)
	kp, err := nkeys.CreateUser()
	if err != nil {
		t.Fatal(err)
	}
	seed, err := kp.Seed()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("NATS_URL", "nats://127.0.0.1:1")
	t.Setenv("NATS_NKEY_SEED", string(seed))

	if bus := connectBusOrLog(); bus != nil {
		bus.close()
		t.Fatal("繋がらないのに consumer を返してはいけない")
	}
}

// --- 実サーバ相手の統合 (NATS_TEST_URL があるときだけ) ---

func TestConsumeAgainstRealServer(t *testing.T) {
	url := os.Getenv("NATS_TEST_URL")
	seed := os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)
	t.Setenv("NATS_DURABLE", "core-driver-test")

	bus, err := connectBus()
	if err != nil {
		t.Fatalf("consumer を張れない: %v", err)
	}
	if bus == nil {
		t.Fatal("設定があるのに nil")
	}
	defer bus.close()

	// 何も無ければ timeout を「エラー無しの 0 件」に翻訳すること
	msgs, err := bus.fetch(4, 500*time.Millisecond)
	if err != nil {
		t.Fatalf("空の fetch はエラーにしない: %v", err)
	}
	if len(msgs) != 0 {
		t.Logf("残っていたイベント %d 件 (ack せず戻す)", len(msgs))
	}
}

func TestConsumeFromForbiddenStreamFails(t *testing.T) {
	// consumer は EVENTS しか読めない。存在しないストリームを黙って成功にすると、
	// 「繋がっているのに何も来ない」状態に気づけない
	url := os.Getenv("NATS_TEST_URL")
	seed := os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)
	t.Setenv("NATS_STREAM", "NOSUCHSTREAM")
	t.Setenv("NATS_FILTER_SUBJECT", "events.raw.>")

	if _, err := connectBus(); err == nil {
		t.Fatal("存在しないストリームへの PullSubscribe は失敗すべき")
	}
}

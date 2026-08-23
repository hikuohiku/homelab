// request_task (コア → heart の依頼) の契約を固定する。
//
// 守りたいのは 4 つ:
//   - 同じ内容の依頼は何度出しても同じ command_id (二重に着手させない)
//   - 空・長すぎる依頼は送らない (heart のキューを汚さない)
//   - publish に失敗したら isError。握り潰すと「依頼しておきました」と嘘をつく
//   - バス未設定でも MCP サーバ自体は動く (読み取りツールは使える)
//
// 実サーバ相手の統合テストは NATS_TEST_URL / NATS_TEST_SEED があるときだけ走る。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"
)

func fixedNow() time.Time {
	return time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
}

func callRequestTask(t *testing.T, s *mcpServer, args string) toolResult {
	t.Helper()
	return s.callTool(context.Background(), "request_task", json.RawMessage(args))
}

func TestCommandIDIsContentAddressed(t *testing.T) {
	a := commandID(commandTypeTaskRequest, "題", "本文")
	b := commandID(commandTypeTaskRequest, "題", "本文")
	c := commandID(commandTypeTaskRequest, "題", "別の本文")
	if a != b {
		t.Fatalf("同じ依頼は同じ id であるべき: %q != %q", a, b)
	}
	if a == c {
		t.Fatal("違う依頼が同じ id になってはいけない")
	}
	if !strings.HasPrefix(a, "core-") {
		t.Fatalf("出所が分かる接頭辞を付ける: %q", a)
	}
}

func TestNewTaskRequestRejectsUnusableInput(t *testing.T) {
	cases := []struct{ name, title, body string }{
		{"題が空", "  ", "本文"},
		{"本文が空", "題", "\n\t "},
		{"題が長すぎる", strings.Repeat("あ", maxCommandTitleRunes+1), "本文"},
		{"本文が長すぎる", "題", strings.Repeat("あ", maxCommandBodyRunes+1)},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if _, err := newTaskRequest(c.title, c.body, fixedNow()); err == nil {
				t.Fatal("送る前に断るべき")
			}
		})
	}
}

func TestNewTaskRequestShape(t *testing.T) {
	ev, err := newTaskRequest("  題  ", " 本文 ", fixedNow())
	if err != nil {
		t.Fatal(err)
	}
	if ev.Title != "題" || ev.Body != "本文" {
		t.Fatalf("前後の空白は落とす: %+v", ev)
	}
	if ev.Type != commandTypeTaskRequest {
		t.Fatalf("種別は task-request 固定: %q", ev.Type)
	}
	if ev.Source != "core" {
		t.Fatalf("出所を残すべき: %q", ev.Source)
	}
	if ev.IssuedAt != "2026-08-23T12:00:00Z" {
		t.Fatalf("発行時刻は UTC の固定書式: %q", ev.IssuedAt)
	}
}

func TestRequestTaskPublishesAndReportsID(t *testing.T) {
	var sent []commandEvent
	s := &mcpServer{
		out:      json.NewEncoder(&strings.Builder{}),
		now:      fixedNow,
		dispatch: func(e commandEvent) error { sent = append(sent, e); return nil },
	}
	res := callRequestTask(t, s, `{"title":"nats の掃除","body":"ストリームが太る"}`)
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}
	if len(sent) != 1 || sent[0].Title != "nats の掃除" {
		t.Fatalf("1 件だけ流すべき: %+v", sent)
	}
	if !strings.Contains(res.Content[0].Text, sent[0].CommandID) {
		t.Fatalf("command_id を返して追跡できるようにする: %q", res.Content[0].Text)
	}
	// 「やっておきます」と安請け合いさせない。採否は heart の判断だと明示する
	if !strings.Contains(res.Content[0].Text, "採択") {
		t.Fatalf("採択されるとは限らないことを伝えるべき: %q", res.Content[0].Text)
	}
}

func TestRequestTaskReportsPublishFailureAsIsError(t *testing.T) {
	s := &mcpServer{
		out:      json.NewEncoder(&strings.Builder{}),
		now:      fixedNow,
		dispatch: func(commandEvent) error { return errors.New("no responders") },
	}
	res := callRequestTask(t, s, `{"title":"題","body":"本文"}`)
	if !res.IsError {
		t.Fatal("流せなかったら isError。握り潰すと起票できていないのに完了と言う")
	}
	if !strings.Contains(res.Content[0].Text, "no responders") {
		t.Fatalf("理由をそのまま見せるべき: %q", res.Content[0].Text)
	}
}

func TestRequestTaskRejectsEmptyArgs(t *testing.T) {
	called := 0
	s := &mcpServer{
		out:      json.NewEncoder(&strings.Builder{}),
		now:      fixedNow,
		dispatch: func(commandEvent) error { called++; return nil },
	}
	if !callRequestTask(t, s, `{}`).IsError {
		t.Fatal("空の依頼は断るべき")
	}
	if !callRequestTask(t, s, `"文字列"`).IsError {
		t.Fatal("引数の形が違えば断るべき")
	}
	if called != 0 {
		t.Fatalf("検証に落ちたら流さない: %d 回流れた", called)
	}
}

func TestRequestTaskWithoutBusIsError(t *testing.T) {
	// バス未設定 (切り戻し構成) でも MCP は動く。依頼だけが「経路が無い」と返る
	t.Setenv("NATS_URL", "")
	t.Setenv("NATS_NKEY_SEED", "")
	s := &mcpServer{out: json.NewEncoder(&strings.Builder{}), now: fixedNow}
	res := callRequestTask(t, s, `{"title":"題","body":"本文"}`)
	if !res.IsError || !strings.Contains(res.Content[0].Text, "経路が無い") {
		t.Fatalf("未設定を成功にしてはいけない: %+v", res)
	}
}

func TestConnectPublisherRejectsBrokenSeed(t *testing.T) {
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("NATS_NKEY_SEED", "SU-これは seed ではない")
	if _, err := connectPublisher(); err == nil {
		t.Fatal("壊れた seed は起動時に落とすべき")
	}
}

// --- 実サーバ相手の統合 (NATS_TEST_URL があるときだけ) ---

func TestPublishAgainstRealServer(t *testing.T) {
	url, seed := os.Getenv("NATS_TEST_URL"), os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)

	p, err := connectPublisher()
	if err != nil || p == nil {
		t.Fatalf("繋げない: %v", err)
	}
	defer p.close()

	ev, err := newTaskRequest("実サーバ疎通", "publish が ack されることを確かめる", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if err := p.publish(ev); err != nil {
		t.Fatalf("publish が ack されない: %v", err)
	}
}

func TestPublishToForbiddenSubjectFails(t *testing.T) {
	// core の鍵は events.heart.> にしか publish できない。ACL 違反が成功に
	// 見えると「送ったつもり」で人間に完了を伝えてしまう
	url, seed := os.Getenv("NATS_TEST_URL"), os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)
	t.Setenv("NATS_COMMAND_SUBJECT_PREFIX", "events.raw.homelab")

	p, err := connectPublisher()
	if err != nil || p == nil {
		t.Fatalf("繋げない: %v", err)
	}
	defer p.close()

	ev, _ := newTaskRequest("権限外", "events.raw へは流せないはず", time.Now())
	if err := p.publish(ev); err == nil {
		t.Fatal("ACL 違反の publish が成功に見えてはいけない")
	}
}

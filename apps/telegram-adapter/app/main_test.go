// telegram-adapter の契約を固定する。
//
//	cd apps/telegram-adapter/app && go test ./...
//
// 固定する契約:
//   - Telegram update → note 形式への変換
//   - 生テキストのまま渡す (trim 等の加工をしない)
//   - allowlist は fail-closed、かつ private チャット限定
//   - note ID が update_id 由来で決定論であること (= 再処理が冪等)
//   - 受信経路の統合 (偽 Telegram API 相手に実 HTTP を流す)
package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func msg(fromID, chatID int64, chatType, text string) Update {
	return Update{
		UpdateID: 100,
		Message: &Message{
			From: &User{ID: fromID},
			Chat: &Chat{ID: chatID, Type: chatType},
			Date: 1756000000,
			Text: text,
		},
	}
}

func TestExtractTextPrefersTextThenCaption(t *testing.T) {
	if got := extractText(msg(1, 1, "private", "hello")); got != "hello" {
		t.Fatalf("text: got %q", got)
	}

	captionOnly := msg(1, 1, "private", "")
	captionOnly.Message.Caption = "写真の説明"
	if got := extractText(captionOnly); got != "写真の説明" {
		t.Fatalf("caption: got %q", got)
	}

	empty := msg(1, 1, "private", "   ")
	if got := extractText(empty); got != "" {
		t.Fatalf("whitespace only should be empty: got %q", got)
	}

	if got := extractText(Update{UpdateID: 1}); got != "" {
		t.Fatalf("no message should be empty: got %q", got)
	}
}

func TestExtractTextDoesNotTrim(t *testing.T) {
	// 生保存が契約。前後の空白や改行を落とさない
	raw := "  止めて\n\n理由: 誤検知  "
	if got := extractText(msg(1, 1, "private", raw)); got != raw {
		t.Fatalf("got %q, want %q", got, raw)
	}
}

func TestIsAllowedFailsClosed(t *testing.T) {
	u := msg(42, 42, "private", "hi")

	if isAllowed(u, 42, false) {
		t.Fatal("hasAllow=false (env 未設定) なら誰も許可してはいけない")
	}
	if !isAllowed(u, 42, true) {
		t.Fatal("allowlist 一致の private DM は許可されるべき")
	}
	if isAllowed(u, 43, true) {
		t.Fatal("送信者 ID 不一致は拒否されるべき")
	}
}

func TestIsAllowedRejectsNonPrivateChats(t *testing.T) {
	// 許可ユーザーが bot をグループに招いた場合でも拾わない。
	// OpenClaw の dmPolicy: allowlist が担っていた遮断をここで持つ
	for _, chatType := range []string{"group", "supergroup", "channel"} {
		u := msg(42, -100, chatType, "hi")
		if isAllowed(u, 42, true) {
			t.Fatalf("chat.type=%s は拒否されるべき", chatType)
		}
	}
}

func TestIsAllowedRejectsMissingFields(t *testing.T) {
	noChat := msg(42, 42, "private", "hi")
	noChat.Message.Chat = nil
	if isAllowed(noChat, 42, true) {
		t.Fatal("chat 欠落は拒否されるべき")
	}

	noFrom := msg(42, 42, "private", "hi")
	noFrom.Message.From = nil
	if isAllowed(noFrom, 42, true) {
		t.Fatal("from 欠落は拒否されるべき")
	}
}

func TestNoteIDIsDeterministic(t *testing.T) {
	at := time.Unix(1756000000, 0).UTC()
	first := noteID(at, 987654)
	second := noteID(at, 987654)
	if first != second {
		t.Fatalf("同じ update_id は同じ ID になるべき: %q != %q", first, second)
	}
	if noteID(at, 987655) == first {
		t.Fatal("update_id が違えば ID も違うべき")
	}
	// route.ts と同じ "YYYYMMDD-HHMMSS-<hex>" の形
	if parts := strings.Split(first, "-"); len(parts) != 3 || len(parts[0]) != 8 || len(parts[1]) != 6 {
		t.Fatalf("ID の形が route.ts と揃っていない: %q", first)
	}
}

func TestBuildNoteMatchesDashboardFormat(t *testing.T) {
	at := time.Unix(1756000000, 0).UTC()
	note := buildNote(at, 7, "本文")

	if note.Source != "telegram" {
		t.Fatalf("source: got %q", note.Source)
	}
	if note.Received != at.Format("2006-01-02T15:04:05Z") {
		t.Fatalf("received: got %q", note.Received)
	}
	if note.Body != "本文" {
		t.Fatalf("body: got %q", note.Body)
	}
}

func TestMessageTimeFallsBackToNow(t *testing.T) {
	now := time.Unix(1756009999, 0).UTC()

	withDate := msg(1, 1, "private", "x")
	if got := messageTime(withDate, now); got.Unix() != 1756000000 {
		t.Fatalf("date を使うべき: got %v", got)
	}

	noDate := msg(1, 1, "private", "x")
	noDate.Message.Date = 0
	if got := messageTime(noDate, now); got.Unix() != now.Unix() {
		t.Fatalf("date 欠落時は now: got %v", got)
	}
}

// --- 統合 (偽 Telegram API に対して実 HTTP を流す) ---

func TestGetUpdatesParsesResult(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if r.FormValue("offset") != "101" {
			t.Errorf("offset: got %q", r.FormValue("offset"))
		}
		if r.FormValue("allowed_updates") != `["message"]` {
			t.Errorf("allowed_updates: got %q", r.FormValue("allowed_updates"))
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true,"result":[{"update_id":101,"message":{"date":1,"text":"hi","from":{"id":5},"chat":{"id":5,"type":"private"}}}]}`))
	}))
	defer server.Close()

	c := newClient(&config{telegramAPI: server.URL, botToken: "tok", pollSeconds: 1})
	updates, err := c.getUpdates(context.Background(), 101)
	if err != nil {
		t.Fatal(err)
	}
	if len(updates) != 1 || updates[0].UpdateID != 101 {
		t.Fatalf("got %+v", updates)
	}
	if extractText(updates[0]) != "hi" {
		t.Fatalf("text: got %q", extractText(updates[0]))
	}
}

// MCP 返信ツールの契約を固定する。
//
// 特に守りたいのは「宛先を引数に取らない」こと。ここが崩れると、注入された指示で
// コアが第三者へ喋れるようになる。
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newTestServer(t *testing.T, handler http.HandlerFunc) (*mcpServer, *strings.Builder, *httptest.Server) {
	t.Helper()
	telegram := httptest.NewServer(handler)
	cfg := &config{
		telegramAPI: telegram.URL, botToken: "tok", pollSeconds: 1,
		allowedUser: 4242, hasAllowUser: true,
	}
	var out strings.Builder
	return &mcpServer{client: newClient(cfg), out: json.NewEncoder(&out)}, &out, telegram
}

func decodeResponses(t *testing.T, raw string) []rpcResponse {
	t.Helper()
	var out []rpcResponse
	for _, line := range strings.Split(strings.TrimSpace(raw), "\n") {
		if line == "" {
			continue
		}
		var r rpcResponse
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatalf("応答を解釈できない %q: %v", line, err)
		}
		out = append(out, r)
	}
	return out
}

func TestInitializeEchoesProtocolVersion(t *testing.T) {
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {})
	defer ts.Close()

	in := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	res := decodeResponses(t, out.String())
	if len(res) != 1 || res[0].Error != nil {
		t.Fatalf("got %+v", res)
	}
	body, _ := json.Marshal(res[0].Result)
	if !strings.Contains(string(body), `"protocolVersion":"2024-11-05"`) {
		t.Fatalf("クライアントの protocolVersion を返すべき: %s", body)
	}
	if !strings.Contains(string(body), `"tools"`) {
		t.Fatalf("tools capability を宣言すべき: %s", body)
	}
}

func TestToolsListExposesOnlyReply(t *testing.T) {
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {})
	defer ts.Close()

	if err := s.serve(context.Background(), strings.NewReader(`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)); err != nil {
		t.Fatal(err)
	}

	var parsed struct {
		Tools []toolDef `json:"tools"`
	}
	body, _ := json.Marshal(decodeResponses(t, out.String())[0].Result)
	if err := json.Unmarshal(body, &parsed); err != nil {
		t.Fatal(err)
	}
	if len(parsed.Tools) != 1 || parsed.Tools[0].Name != "telegram_reply" {
		t.Fatalf("ツールは telegram_reply 1 つだけ: %+v", parsed.Tools)
	}

	// 宛先を引数に取らないこと。ここが増えたら設計が崩れている
	schema, _ := json.Marshal(parsed.Tools[0].InputSchema)
	for _, forbidden := range []string{"chat_id", "chatId", "to", "recipient"} {
		if strings.Contains(string(schema), forbidden) {
			t.Fatalf("宛先を引数に取ってはいけない (%s): %s", forbidden, schema)
		}
	}
}

func TestToolCallSendsToAllowlistedChatOnly(t *testing.T) {
	var gotChatID, gotText string
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		gotChatID = r.FormValue("chat_id")
		gotText = r.FormValue("text")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true,"result":{"message_id":99}}`))
	})
	defer ts.Close()

	// 引数に chat_id を紛れ込ませても無視され、allowlist の宛先へ送られること
	in := `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"telegram_reply","arguments":{"text":"確認しました","chat_id":"-1009999"}}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	if gotChatID != "4242" {
		t.Fatalf("allowlist の宛先へ送るべき: got %q", gotChatID)
	}
	if gotText != "確認しました" {
		t.Fatalf("text: got %q", gotText)
	}

	var result toolResult
	body, _ := json.Marshal(decodeResponses(t, out.String())[0].Result)
	if err := json.Unmarshal(body, &result); err != nil {
		t.Fatal(err)
	}
	if result.IsError {
		t.Fatalf("成功のはず: %+v", result)
	}
	if !strings.Contains(result.Content[0].Text, "99") {
		t.Fatalf("message_id を返すべき: %+v", result)
	}
}

func TestToolCallReportsFailureAsIsError(t *testing.T) {
	// 送信失敗を JSON-RPC error にすると、モデルが失敗を観測できず
	// 「届いたつもり」になる。isError でツール結果として返すこと
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"ok":false,"description":"chat not found"}`))
	})
	defer ts.Close()

	in := `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"telegram_reply","arguments":{"text":"hi"}}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	res := decodeResponses(t, out.String())[0]
	if res.Error != nil {
		t.Fatalf("JSON-RPC error ではなく isError で返すべき: %+v", res.Error)
	}
	var result toolResult
	body, _ := json.Marshal(res.Result)
	_ = json.Unmarshal(body, &result)
	if !result.IsError {
		t.Fatalf("isError であるべき: %+v", result)
	}
}

func TestSendReplyRejectsEmptyAndTooLong(t *testing.T) {
	s, _, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("送信してはいけない")
	})
	defer ts.Close()

	if _, err := s.client.sendReply(context.Background(), "   "); err == nil {
		t.Fatal("空文は拒否すべき")
	}
	if _, err := s.client.sendReply(context.Background(), strings.Repeat("あ", maxReplyRunes+1)); err == nil {
		t.Fatal("長すぎる本文は拒否すべき")
	}
}

func TestSendReplyFailsClosedWithoutAllowlist(t *testing.T) {
	cfg := &config{telegramAPI: "http://127.0.0.1:1", botToken: "t", pollSeconds: 1, hasAllowUser: false}
	if _, err := newClient(cfg).sendReply(context.Background(), "hi"); err == nil {
		t.Fatal("allowlist 未設定なら送信してはいけない")
	}
}

func TestNotificationsGetNoResponse(t *testing.T) {
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {})
	defer ts.Close()

	in := `{"jsonrpc":"2.0","method":"notifications/initialized"}` + "\n"
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(out.String()) != "" {
		t.Fatalf("通知には応答しない: %q", out.String())
	}
}

func TestUnknownMethodWithIDGetsError(t *testing.T) {
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {})
	defer ts.Close()

	in := `{"jsonrpc":"2.0","id":9,"method":"resources/list"}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	res := decodeResponses(t, out.String())[0]
	if res.Error == nil || res.Error.Code != -32601 {
		t.Fatalf("-32601 を返すべき: %+v", res)
	}
}

func TestMalformedLineIsSkipped(t *testing.T) {
	s, out, ts := newTestServer(t, func(w http.ResponseWriter, r *http.Request) {})
	defer ts.Close()

	in := "{ not json\n" + `{"jsonrpc":"2.0","id":10,"method":"ping"}` + "\n"
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	res := decodeResponses(t, out.String())
	if len(res) != 1 || res[0].Error != nil {
		t.Fatalf("壊れた行は捨てて次を処理すべき: %+v", res)
	}
}

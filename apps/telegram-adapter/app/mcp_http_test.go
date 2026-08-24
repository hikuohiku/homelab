// HTTP streamable 転送の契約を固定する。
//
// 中身は stdio と同じ handle なので、ここで見るのは HTTP の作法と、
// **転送を変えても宛先が固定のままであること**。remote 化で第三者へ喋れるように
// なっていたら、この移行の意味が無い。
package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newHTTPMCP(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	telegram := httptest.NewServer(handler)
	t.Cleanup(telegram.Close)
	cfg := &config{
		telegramAPI: telegram.URL, botToken: "tok", pollSeconds: 1,
		allowedUser: 4242, hasAllowUser: true,
	}
	s := &mcpServer{client: newClient(cfg), out: json.NewEncoder(&strings.Builder{})}
	srv := httptest.NewServer(s.httpMux())
	t.Cleanup(srv.Close)
	return srv
}

func postMCP(t *testing.T, srv *httptest.Server, body string) (*http.Response, []byte) {
	t.Helper()
	resp, err := http.Post(srv.URL+"/mcp", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp, raw
}

func TestHTTPInitializeAndToolsList(t *testing.T) {
	srv := newHTTPMCP(t, func(w http.ResponseWriter, r *http.Request) {})

	resp, raw := postMCP(t, srv, `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}`)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("initialize: status %d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("application/json で返すこと: %q", ct)
	}
	if !strings.Contains(string(raw), `"protocolVersion":"2025-06-18"`) {
		t.Fatalf("クライアントの protocolVersion を返すべき: %s", raw)
	}

	_, raw = postMCP(t, srv, `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)
	var parsed struct {
		Result struct {
			Tools []toolDef `json:"tools"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	if len(parsed.Result.Tools) != 1 || parsed.Result.Tools[0].Name != "telegram_reply" {
		t.Fatalf("ツールは telegram_reply 1 つだけ: %+v", parsed.Result.Tools)
	}
	schema, _ := json.Marshal(parsed.Result.Tools[0].InputSchema)
	for _, forbidden := range []string{"chat_id", "chatId", "to", "recipient"} {
		if strings.Contains(string(schema), forbidden) {
			t.Fatalf("宛先を引数に取ってはいけない (%s): %s", forbidden, schema)
		}
	}
}

func TestHTTPToolCallStillSendsToAllowlistedChatOnly(t *testing.T) {
	var gotChatID, gotText string
	srv := newHTTPMCP(t, func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		gotChatID = r.FormValue("chat_id")
		gotText = r.FormValue("text")
		_, _ = w.Write([]byte(`{"ok":true,"result":{"message_id":99}}`))
	})

	_, raw := postMCP(t, srv,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"telegram_reply","arguments":{"text":"確認しました","chat_id":"-1009999"}}}`)

	if gotChatID != "4242" {
		t.Fatalf("転送を変えても宛先は allowlist の所有者に固定: got %q", gotChatID)
	}
	if gotText != "確認しました" {
		t.Fatalf("text: got %q", gotText)
	}
	var parsed struct {
		Result toolResult `json:"result"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Result.IsError || !strings.Contains(parsed.Result.Content[0].Text, "99") {
		t.Fatalf("成功として message_id を返すべき: %+v", parsed.Result)
	}
}

func TestHTTPNotificationGets202(t *testing.T) {
	srv := newHTTPMCP(t, func(w http.ResponseWriter, r *http.Request) {})
	resp, raw := postMCP(t, srv, `{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("通知は 202 で本文なし: status=%d body=%s", resp.StatusCode, raw)
	}
	if len(strings.TrimSpace(string(raw))) != 0 {
		t.Fatalf("通知に応答してはいけない: %s", raw)
	}
}

func TestHTTPGetIsRejected(t *testing.T) {
	srv := newHTTPMCP(t, func(w http.ResponseWriter, r *http.Request) {})
	resp, err := http.Get(srv.URL + "/mcp")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("SSE ストリームは開かない (405): %d", resp.StatusCode)
	}
}

func TestHTTPHealthzReportsBoot(t *testing.T) {
	srv := newHTTPMCP(t, func(w http.ResponseWriter, r *http.Request) {})
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed struct {
		Boot string `json:"boot"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Boot != bootID {
		t.Fatalf("boot を返すこと (driver の再接続判定に使う): %+v", parsed)
	}
}

func TestParseMCPListen(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
		bad  bool
	}{
		{args: nil, want: ""},
		{args: []string{"--listen", "127.0.0.1:4097"}, want: "127.0.0.1:4097"},
		{args: []string{"--listen=127.0.0.1:4097"}, want: "127.0.0.1:4097"},
		{args: []string{"--listen"}, bad: true},
		{args: []string{"--listen", "4097"}, bad: true},
		{args: []string{"--http"}, bad: true},
	} {
		got, err := parseMCPListen(tc.args)
		if tc.bad {
			if err == nil {
				t.Fatalf("%v は拒否すべき (黙って stdio で起動すると誰にも繋がらない)", tc.args)
			}
			continue
		}
		if err != nil || got != tc.want {
			t.Fatalf("%v: got %q %v, want %q", tc.args, got, err, tc.want)
		}
	}
}

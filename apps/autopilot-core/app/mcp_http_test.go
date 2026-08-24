// HTTP streamable 転送の契約を固定する。
//
// stdio と同じ handle を使うので、ツールの中身はここでは見ない。見るのは
// 「同じ JSON-RPC が HTTP でも通ること」と、opencode の MCP クライアントが
// 前提にしている HTTP の作法 (通知は 202、GET は 405)。
package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

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

func newHTTPMCP(t *testing.T, cfg *config) *httptest.Server {
	t.Helper()
	s := &mcpServer{client: newClient(cfg), out: json.NewEncoder(&strings.Builder{})}
	srv := httptest.NewServer(s.httpMux())
	t.Cleanup(srv.Close)
	return srv
}

func TestHTTPInitializeAndToolsList(t *testing.T) {
	srv := newHTTPMCP(t, &config{})

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
	names := map[string]bool{}
	for _, tool := range parsed.Result.Tools {
		names[tool.Name] = true
	}
	for _, want := range []string{"homelab_status", "homelab_health", "request_task"} {
		if !names[want] {
			t.Fatalf("%s が HTTP 越しに見えない: %+v", want, names)
		}
	}
}

func TestHTTPToolCallReachesTheSameTool(t *testing.T) {
	dashboard := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"agents":[]}`))
	}))
	defer dashboard.Close()
	t.Setenv("CORE_DASHBOARD_URL", dashboard.URL)

	srv := newHTTPMCP(t, &config{})
	_, raw := postMCP(t, srv,
		`{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"homelab_status","arguments":{}}}`)

	var parsed struct {
		Result toolResult `json:"result"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Result.IsError {
		t.Fatalf("成功のはず: %+v", parsed.Result)
	}
	if !strings.Contains(parsed.Result.Content[0].Text, `"agents"`) {
		t.Fatalf("取得した本文をそのまま返すべき: %+v", parsed.Result)
	}
}

func TestHTTPNotificationGets202(t *testing.T) {
	srv := newHTTPMCP(t, &config{})
	resp, raw := postMCP(t, srv, `{"jsonrpc":"2.0","method":"notifications/initialized"}`)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("通知は 202 で本文なし: status=%d body=%s", resp.StatusCode, raw)
	}
	if len(strings.TrimSpace(string(raw))) != 0 {
		t.Fatalf("通知に応答してはいけない: %s", raw)
	}
}

func TestHTTPGetIsRejected(t *testing.T) {
	srv := newHTTPMCP(t, &config{})
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
	srv := newHTTPMCP(t, &config{})
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

func TestHTTPMalformedBodyGetsParseError(t *testing.T) {
	srv := newHTTPMCP(t, &config{})
	resp, raw := postMCP(t, srv, `{ not json`)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("400 を返すべき: %d", resp.StatusCode)
	}
	if !strings.Contains(string(raw), "-32700") {
		t.Fatalf("parse error を返すべき: %s", raw)
	}
}

func TestParseMCPListen(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
		bad  bool
	}{
		{args: nil, want: ""},
		{args: []string{"--listen", "127.0.0.1:4098"}, want: "127.0.0.1:4098"},
		{args: []string{"--listen=127.0.0.1:4098"}, want: "127.0.0.1:4098"},
		{args: []string{"--listen"}, bad: true},
		{args: []string{"--listen", "4098"}, bad: true},
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

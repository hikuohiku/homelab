// コアの「目」(MCP 観測ツール) の契約を固定する。
//
// 守りたいのは 2 点:
//   - 引数を取らないこと。取れる先をコードで固定しておく
//   - 取得失敗を isError で返すこと。握り潰すと「取れなかった」が「異常なし」に化ける
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newMCP(t *testing.T, cfg *config) (*mcpServer, *strings.Builder) {
	t.Helper()
	var out strings.Builder
	return &mcpServer{client: newClient(cfg), out: json.NewEncoder(&out)}, &out
}

func firstResponse(t *testing.T, raw string) rpcResponse {
	t.Helper()
	line := strings.SplitN(strings.TrimSpace(raw), "\n", 2)[0]
	var r rpcResponse
	if err := json.Unmarshal([]byte(line), &r); err != nil {
		t.Fatalf("応答を解釈できない %q: %v", line, err)
	}
	return r
}

func resultOf(t *testing.T, r rpcResponse) toolResult {
	t.Helper()
	var out toolResult
	body, _ := json.Marshal(r.Result)
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestToolsListKeepsWindowsNarrow(t *testing.T) {
	s, out := newMCP(t, &config{})
	if err := s.serve(context.Background(), strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`)); err != nil {
		t.Fatal(err)
	}

	var parsed struct {
		Tools []toolDef `json:"tools"`
	}
	body, _ := json.Marshal(firstResponse(t, out.String()).Result)
	if err := json.Unmarshal(body, &parsed); err != nil {
		t.Fatal(err)
	}

	names := map[string]bool{}
	for _, tool := range parsed.Tools {
		names[tool.Name] = true
		schema, _ := json.Marshal(tool.InputSchema)
		// 観測ツールは引数を取らせない。URL やクエリを渡せるようにすると、
		// 「用途を固定した窓」という性質が崩れる
		if tool.Name != "request_task" && tool.Name != "dispatch_task" &&
			strings.Contains(string(schema), "required") {
			t.Fatalf("%s は引数を取らないべき: %s", tool.Name, schema)
		}
		// 依頼ツールも「何を頼むか」以外の自由度を持たない。宛先・Job 種別・
		// モデル・優先度を選べるようにすると heart の判断領域を侵す
		for _, forbidden := range []string{"url", "path", "query", "command", "model", "priority", "job"} {
			if strings.Contains(string(schema), forbidden) {
				t.Fatalf("%s に %s を渡せてはいけない: %s", tool.Name, forbidden, schema)
			}
		}
	}
	// 窓は増えたが、増えたのはすべて「引数を取らない読み取り」。
	// ここに引数付きの汎用ツール (kubectl / http fetch) が混ざったら失敗させる
	want := []string{
		"homelab_status", "homelab_health",
		"homelab_applications", "homelab_pods", "homelab_events",
		"request_task", "dispatch_task",
	}
	if len(parsed.Tools) != len(want) {
		t.Fatalf("窓は %d 個だけ: %+v", len(want), parsed.Tools)
	}
	for _, n := range want {
		if !names[n] {
			t.Fatalf("%s が無い: %+v", n, parsed.Tools)
		}
	}
}

func TestStatusReturnsSnapshotBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/snapshot" {
			t.Errorf("path: got %q", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"heart":{"at":"2026-08-23T12:00:00Z"},"agents":[]}`))
	}))
	defer server.Close()
	t.Setenv("CORE_DASHBOARD_URL", server.URL)

	s, out := newMCP(t, &config{})
	in := `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"homelab_status"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	res := resultOf(t, firstResponse(t, out.String()))
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}
	// 加工せずそのまま返すこと (要約はコアの仕事)
	if !strings.Contains(res.Content[0].Text, `"heart"`) {
		t.Fatalf("取得した JSON をそのまま返すべき: %q", res.Content[0].Text)
	}
}

func TestStatusReportsUnreachableAsIsError(t *testing.T) {
	// 「取れなかった」を成功として返すと、コアが「異常なし」と答えてしまう
	t.Setenv("CORE_DASHBOARD_URL", "http://127.0.0.1:1")

	s, out := newMCP(t, &config{})
	in := `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"homelab_status"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	res := resultOf(t, firstResponse(t, out.String()))
	if !res.IsError {
		t.Fatalf("到達不能は isError で返すべき: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, "取得できなかった") {
		t.Fatalf("理由を伝えるべき: %q", res.Content[0].Text)
	}
}

func TestStatusReportsBadStatusAsIsError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	t.Setenv("CORE_DASHBOARD_URL", server.URL)

	s, out := newMCP(t, &config{})
	in := `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"homelab_status"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	if !resultOf(t, firstResponse(t, out.String())).IsError {
		t.Fatal("5xx は isError で返すべき")
	}
}

func TestHealthReadsReportConfigMap(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/namespaces/autopilot/configmaps/ops-health-report" {
			t.Errorf("reporter が書く ConfigMap を見るべき: %q", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"data":{"latest.json":"{\"generated_at\":\"2026-08-23T12:00:00Z\",\"applications\":[]}"}}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	in := `{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"homelab_health"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	res := resultOf(t, firstResponse(t, out.String()))
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, "generated_at") {
		t.Fatalf("レポートをそのまま返すべき: %q", res.Content[0].Text)
	}
}

// レポートが読めないことを「異常なし」に化けさせない。
func TestHealthReportsMissingKeyAsIsError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"data":{}}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	in := `{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"homelab_health"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	if !resultOf(t, firstResponse(t, out.String())).IsError {
		t.Fatal("latest.json が無ければ isError で返すべき")
	}
}

func TestUnknownToolIsError(t *testing.T) {
	s, out := newMCP(t, &config{})
	in := `{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"kubectl_delete"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	if !resultOf(t, firstResponse(t, out.String())).IsError {
		t.Fatal("知らないツールは isError で断るべき")
	}
}

func TestClipBoundsResultSize(t *testing.T) {
	// dashboard の snapshot が育ってもコアの文脈を食い潰さない
	long := strings.Repeat("x", maxToolResultBytes+500)
	got := clip(long)
	if len(got) <= maxToolResultBytes || !strings.Contains(got, "切り詰めた") {
		t.Fatalf("切り詰めた旨を添えるべき: len=%d", len(got))
	}
	if clip("short") != "short" {
		t.Fatal("短いものは触らない")
	}
}

func TestInitializeAndNotifications(t *testing.T) {
	s, out := newMCP(t, &config{})
	in := `{"jsonrpc":"2.0","id":7,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}` + "\n" +
		`{"jsonrpc":"2.0","method":"notifications/initialized"}` + "\n"
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}

	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("通知には応答しない: %v", lines)
	}
	body, _ := json.Marshal(firstResponse(t, out.String()).Result)
	if !strings.Contains(string(body), `"protocolVersion":"2024-11-05"`) {
		t.Fatalf("クライアントの protocolVersion を返すべき: %s", body)
	}
}

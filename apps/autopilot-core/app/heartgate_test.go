// dispatch_task (heart の admission gate を叩く口) の契約。
//
// 守りたいのは 3 点:
//   - 拒否を成功として返さないこと。握り潰すと、コアが着手していないのに
//     「着手しました」と人間に言う
//   - heart が居なくても壊れず、request_task (冷スペア) に案内すること
//   - 入力が最小のままであること (実行役の種別・思考エンジン・優先度を選ばせない)
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func callDispatch(t *testing.T, s *mcpServer, out *strings.Builder, args string) toolResult {
	t.Helper()
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"dispatch_task","arguments":` + args + `}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	return resultOf(t, firstResponse(t, out.String()))
}

const goodArgs = `{"title":"ops-dashboard の 500 を直す","body":"snapshot API が 500 を返している"}`

func gateServer(t *testing.T, status int, body string) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/dispatch" {
			t.Errorf("gate の口は /dispatch だけ: %q", r.URL.Path)
		}
		if r.Method != http.MethodPost {
			t.Errorf("POST で頼むこと: %q", r.Method)
		}
		var got gateRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("要求を解釈できない: %v", err)
		}
		if got.Title == "" || got.Body == "" {
			t.Errorf("検証に落ちた要求を送っている: %+v", got)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	t.Setenv("CORE_HEART_GATE_URL", server.URL)
	return server
}

func TestDispatchAcceptedTellsWhichProject(t *testing.T) {
	gateServer(t, http.StatusAccepted,
		`{"status":"accepted","message":"受理しました (P-9000)。","dispatch_id":"d-abc","project_id":"P-9000"}`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if res.IsError {
		t.Fatalf("受理は成功として返すべき: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, "P-9000") {
		t.Fatalf("どのプロジェクトになったかを返すべき: %q", res.Content[0].Text)
	}
}

func TestDispatchDeniedIsNotSuccess(t *testing.T) {
	// 「止めて」が効いているとき。ここを成功で返すと、コアが着手したと錯覚する
	gateServer(t, http.StatusConflict,
		`{"status":"denied","reason":"stop_engaged","message":"人間が全停止を指示しています"}`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if !res.IsError {
		t.Fatalf("拒否は isError で返すべき: %+v", res)
	}
	text := res.Content[0].Text
	// 理由が人語で分かること
	if !strings.Contains(text, "stop_engaged") || !strings.Contains(text, "全停止") {
		t.Fatalf("拒否の理由を人語で返すべき: %q", text)
	}
}

func TestDispatchCapacityDenialCarriesTheReason(t *testing.T) {
	gateServer(t, http.StatusConflict,
		`{"status":"denied","reason":"capacity","message":"同時走行の上限 2 本に達しています"}`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if !res.IsError {
		t.Fatal("上限での拒否も isError")
	}
	if !strings.Contains(res.Content[0].Text, "上限") {
		t.Fatalf("スロット満杯だと分かること: %q", res.Content[0].Text)
	}
}

func TestDispatchDuplicateIsNotAnError(t *testing.T) {
	// 冪等に畳んだ、という正常な応答。失敗ではない
	gateServer(t, http.StatusOK,
		`{"status":"duplicate","reason":"already_dispatched","message":"同じ要求は既に受理済みです (P-9000)"}`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if res.IsError {
		t.Fatalf("重複は失敗ではない: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, "既に受理済み") {
		t.Fatalf("既出だと分かること: %q", res.Content[0].Text)
	}
}

func TestDispatchWithoutHeartFallsBackToRequestTask(t *testing.T) {
	// heart が落ちていてもコアは壊れない。冷スペアの経路を必ず案内する
	t.Setenv("CORE_HEART_GATE_URL", "http://127.0.0.1:1")

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if !res.IsError {
		t.Fatalf("到達不能は isError で返すべき: %+v", res)
	}
	text := res.Content[0].Text
	if !strings.Contains(text, "request_task") {
		t.Fatalf("冷スペアの経路を案内すべき: %q", text)
	}
	if !strings.Contains(text, "着手したとは言わない") {
		t.Fatalf("着手したと錯覚させないこと: %q", text)
	}
}

func TestDispatchRejectsIncompleteRequestsWithoutCallingHeart(t *testing.T) {
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer server.Close()
	t.Setenv("CORE_HEART_GATE_URL", server.URL)

	for name, args := range map[string]string{
		"title 無し": `{"title":"","body":"b"}`,
		"body 無し":  `{"title":"t","body":""}`,
	} {
		s, out := newMCP(t, &config{})
		res := callDispatch(t, s, out, args)
		if !res.IsError {
			t.Fatalf("%s: 不完全な要求は断るべき: %+v", name, res)
		}
	}
	if called {
		t.Fatal("検証に落ちた要求を heart へ送ってはいけない")
	}
}

func TestNewGateRequestTrimsAndBoundsInput(t *testing.T) {
	req, err := newGateRequest("  直す  ", " 壊れている ")
	if err != nil {
		t.Fatal(err)
	}
	if req.Title != "直す" || req.Body != "壊れている" {
		t.Fatalf("前後の空白は落とすこと: %+v", req)
	}
	if _, err := newGateRequest(strings.Repeat("あ", maxCommandTitleRunes+1), "b"); err == nil {
		t.Fatal("長すぎる題名は断ること")
	}
	if _, err := newGateRequest("t", strings.Repeat("あ", maxCommandBodyRunes+1)); err == nil {
		t.Fatal("長すぎる本文は断ること")
	}
}

// dispatch_task のスキーマは title と body の 2 つだけ。
func TestDispatchSchemaIsTitleAndBodyOnly(t *testing.T) {
	for _, tool := range toolDefs() {
		if tool.Name != "dispatch_task" {
			continue
		}
		schema, _ := json.Marshal(tool.InputSchema)
		if strings.Contains(string(schema), "verify") {
			t.Fatalf("verify は取らない: %s", schema)
		}
		if !strings.Contains(string(schema), `"required":["title","body"]`) {
			t.Fatalf("必須は title と body だけ: %s", schema)
		}
		return
	}
	t.Fatal("dispatch_task が tools/list に無い")
}

// dispatch_task は verify を取らない (2026-08-24 の所有者判断)。
// 送られてきても引数として存在しないので、要求には載らない。
func TestDispatchTakesNoVerify(t *testing.T) {
	gateServer(t, http.StatusAccepted,
		`{"status":"accepted","message":"受理しました (P-9000)。","dispatch_id":"d-abc","project_id":"P-9000"}`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out,
		`{"title":"直す","body":"壊れている","verify":["test -f x"]}`)
	if res.IsError {
		t.Fatalf("verify が付いていても受理を妨げないこと: %+v", res)
	}
}

func TestHeartGateURLDefaultsToTheClusterService(t *testing.T) {
	t.Setenv("CORE_HEART_GATE_URL", "")
	got := heartGateURL()
	// cluster 内の Service だけ。Ingress も Tailscale も通さない
	if !strings.HasPrefix(got, "http://autopilot-heart.autopilot.svc:") {
		t.Fatalf("cluster 内の Service を既定にすること: %q", got)
	}
	if !strings.HasSuffix(got, "/dispatch") {
		t.Fatalf("叩く口は /dispatch: %q", got)
	}
}

func TestDispatchGarbledResponseIsAnError(t *testing.T) {
	gateServer(t, http.StatusOK, `not json`)

	s, out := newMCP(t, &config{})
	res := callDispatch(t, s, out, goodArgs)
	if !res.IsError {
		t.Fatal("解釈できない応答を成功として返さないこと")
	}
}

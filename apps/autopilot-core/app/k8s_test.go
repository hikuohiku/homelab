// live の k8s 読み取り 3 種の契約を固定する。
//
// 守りたいのは mcp_test.go と同じ 2 点:
//   - 引数を取らないこと (取れる先をコードで固定する)
//   - 取れなかったことを isError で返すこと。403 を「異常なし」に化けさせない
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// newKubeAgainst は httptest のサーバを向く kubeClient を返す。
// トークン読み込みは差し替える (テスト環境に projected volume は無い)。
func newKubeAgainst(base string) *kubeClient {
	return &kubeClient{
		base:  base,
		token: func() (string, error) { return "test-token", nil },
		http:  &http.Client{Timeout: 5 * time.Second},
		now:   func() time.Time { return time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC) },
	}
}

func callToolWith(t *testing.T, s *mcpServer, out *strings.Builder, name string) toolResult {
	t.Helper()
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"` + name + `"}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	return resultOf(t, firstResponse(t, out.String()))
}

func TestApplicationsSummarisesLiveState(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/apis/argoproj.io/v1alpha1/applications") {
			t.Errorf("Application を見るべき: %q", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer test-token" {
			t.Errorf("SA トークンを付けるべき: %q", r.Header.Get("Authorization"))
		}
		_, _ = w.Write([]byte(`{"items":[
			{"metadata":{"name":"immich"},"status":{"sync":{"status":"Synced","revision":"0123456789abcdef"},"health":{"status":"Healthy"}}},
			{"metadata":{"name":"autopilot"},"status":{"sync":{"status":"OutOfSync"},"health":{"status":"Degraded","message":"boom"}}}
		]}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	res := callToolWith(t, s, out, "homelab_applications")
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}

	var parsed struct {
		Total        int `json:"total"`
		Degraded     int `json:"degraded"`
		Applications []struct {
			Name     string `json:"name"`
			Sync     string `json:"sync"`
			Health   string `json:"health"`
			Revision string `json:"revision"`
		} `json:"applications"`
	}
	if err := json.Unmarshal([]byte(res.Content[0].Text), &parsed); err != nil {
		t.Fatalf("JSON を返すべき: %v (%q)", err, res.Content[0].Text)
	}
	if parsed.Total != 2 || parsed.Degraded != 1 {
		t.Fatalf("total=2 degraded=1 のはず: %+v", parsed)
	}
	// 名前順。immich より autopilot が先
	if parsed.Applications[0].Name != "autopilot" {
		t.Fatalf("名前順に並べるべき: %+v", parsed.Applications)
	}
	if parsed.Applications[1].Revision != "01234567" {
		t.Fatalf("revision は短縮するべき: %q", parsed.Applications[1].Revision)
	}
}

func TestPodsFoldsEachPodToOneRow(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"items":[
			{"metadata":{"name":"a","namespace":"autopilot"},"spec":{"nodeName":"node01"},
			 "status":{"phase":"Running","containerStatuses":[{"ready":true,"restartCount":0,"state":{"running":{}}}]}},
			{"metadata":{"name":"b","namespace":"autopilot"},
			 "status":{"phase":"Running","containerStatuses":[
				{"ready":false,"restartCount":7,"state":{"waiting":{"reason":"CrashLoopBackOff"}}},
				{"ready":true,"restartCount":1,"state":{"running":{}}}]}}
		]}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	res := callToolWith(t, s, out, "homelab_pods")
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}

	var parsed struct {
		Total     int `json:"total"`
		Unhealthy int `json:"unhealthy"`
		Pods      []struct {
			Name     string `json:"name"`
			Ready    string `json:"ready"`
			Restarts int    `json:"restarts"`
			Reason   string `json:"reason"`
		} `json:"pods"`
	}
	if err := json.Unmarshal([]byte(res.Content[0].Text), &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Total != 2 || parsed.Unhealthy != 1 {
		t.Fatalf("total=2 unhealthy=1 のはず: %+v", parsed)
	}
	if parsed.Pods[1].Ready != "1/2" || parsed.Pods[1].Restarts != 8 {
		t.Fatalf("ready と再起動回数を畳むべき: %+v", parsed.Pods[1])
	}
	if parsed.Pods[1].Reason != "CrashLoopBackOff" {
		t.Fatalf("止まっている理由を残すべき: %+v", parsed.Pods[1])
	}
}

func TestEventsAreNewestFirstAndWarningOnly(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Normal を除くのは server 側の fieldSelector。ここで落ちると
		// 全 Event が流れ込んで文脈を食い潰す
		if !strings.Contains(r.URL.RawQuery, "type%21%3DNormal") {
			t.Errorf("type!=Normal で絞るべき: %q", r.URL.RawQuery)
		}
		_, _ = w.Write([]byte(`{"items":[
			{"type":"Warning","reason":"OOMKilling","message":"old","lastTimestamp":"2026-08-24T10:00:00Z",
			 "metadata":{"namespace":"autopilot"},"involvedObject":{"kind":"Pod","name":"a"}},
			{"type":"Warning","reason":"FailedScheduling","message":"new","lastTimestamp":"2026-08-24T11:00:00Z",
			 "metadata":{"namespace":"autopilot"},"involvedObject":{"kind":"Pod","name":"b"}}
		]}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	res := callToolWith(t, s, out, "homelab_events")
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}

	var parsed struct {
		Returned int `json:"returned"`
		Events   []struct {
			Reason  string `json:"reason"`
			Object  string `json:"object"`
			Message string `json:"message"`
		} `json:"events"`
	}
	if err := json.Unmarshal([]byte(res.Content[0].Text), &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Returned != 2 || parsed.Events[0].Message != "new" {
		t.Fatalf("新しい順で返すべき: %+v", parsed)
	}
	if parsed.Events[0].Object != "autopilot/pod b" {
		t.Fatalf("どの対象かが分かる形にするべき: %q", parsed.Events[0].Object)
	}
}

func TestForbiddenIsReportedAsIsError(t *testing.T) {
	// RBAC が足りないことを「異常なし」に化けさせない。ここが握り潰されると、
	// コアは「Pod は全部元気です」と嘘をつく
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"pods is forbidden"}`))
	}))
	defer server.Close()

	for _, name := range []string{"homelab_applications", "homelab_pods", "homelab_events"} {
		s, out := newMCP(t, &config{})
		s.kube = newKubeAgainst(server.URL)
		res := callToolWith(t, s, out, name)
		if !res.IsError {
			t.Fatalf("%s: 403 は isError で返すべき: %+v", name, res)
		}
		if !strings.Contains(res.Content[0].Text, "取得できなかった") {
			t.Fatalf("%s: 理由を伝えるべき: %q", name, res.Content[0].Text)
		}
	}
}

func TestMissingTokenIsReportedAsIsError(t *testing.T) {
	// projected volume が付いていないサイドカーで起きること。
	// 起動を止めずに、呼ばれたときだけ「見えない」と言う
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	t.Setenv("CORE_KUBE_API", "")

	s, out := newMCP(t, &config{})
	res := callToolWith(t, s, out, "homelab_pods")
	if !res.IsError {
		t.Fatalf("k8s へ届かない構成では isError を返すべき: %+v", res)
	}
}

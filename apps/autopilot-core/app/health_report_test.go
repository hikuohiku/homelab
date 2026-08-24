// homelab_health の取得経路を固定する (設計 state-out-of-git Phase 5)。
//
// 守りたいのは 3 点:
//   - 既定はクラスタ内の ConfigMap。GitHub には出ない
//   - ConfigMap が読めないときだけブランチに落ちる
//   - 両方読めなければ isError。取れなかったことを「異常なし」に化けさせない
package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const healthReportJSON = `{"generated_at":"2026-08-25T00:00:00Z","applications":[{"name":"immich","sync":"Synced","health":"Healthy"}]}`

func TestHealthComesFromTheConfigMapNotGitHub(t *testing.T) {
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		want := "/api/v1/namespaces/ops-health-reporter/configmaps/ops-health-report"
		if r.URL.Path != want {
			t.Errorf("ConfigMap を見るべき: %q", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"data":{"latest.json":` + quoteJSON(healthReportJSON) + `}}`))
	}))
	defer kube.Close()

	github := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("ConfigMap が読めたのに GitHub を叩いた: %q", r.URL.Path)
	}))
	defer github.Close()

	s, out := newMCP(t, &config{githubAPI: github.URL, repo: "o/r"})
	s.kube = newKubeAgainst(kube.URL)
	res := callToolWith(t, s, out, "homelab_health")
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, `"immich"`) {
		t.Fatalf("レポートをそのまま返すべき: %q", res.Content[0].Text)
	}
}

func TestHealthFallsBackToTheBranchWhenTheConfigMapIsUnreadable(t *testing.T) {
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"forbidden"}`))
	}))
	defer kube.Close()

	github := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.RawQuery, "ref=ops-health-report") {
			t.Errorf("ブランチを読むべき: %q", r.URL.String())
		}
		_, _ = w.Write([]byte(healthReportJSON))
	}))
	defer github.Close()

	s, out := newMCP(t, &config{githubAPI: github.URL, repo: "o/r"})
	s.kube = newKubeAgainst(kube.URL)
	res := callToolWith(t, s, out, "homelab_health")
	if res.IsError {
		t.Fatalf("ブランチに落ちて成功するはず: %+v", res)
	}
	if !strings.Contains(res.Content[0].Text, `"immich"`) {
		t.Fatalf("レポートをそのまま返すべき: %q", res.Content[0].Text)
	}
}

func TestHealthIsAnErrorWhenNeitherRouteAnswers(t *testing.T) {
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer kube.Close()
	github := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer github.Close()

	s, out := newMCP(t, &config{githubAPI: github.URL, repo: "o/r"})
	s.kube = newKubeAgainst(kube.URL)
	res := callToolWith(t, s, out, "homelab_health")
	if !res.IsError {
		t.Fatalf("読めなかったのだから isError のはず: %+v", res)
	}
}

// driver 側 (main.go の watchHealth) も同じ経路で読む。
func TestDriverReadsHealthFromTheConfigMap(t *testing.T) {
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":{"latest.json":` + quoteJSON(healthReportJSON) + `}}`))
	}))
	defer kube.Close()
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("ConfigMap が読めたのに GitHub を叩いた: %q", r.URL.Path)
	}))
	defer gh.Close()

	c := newClient(&config{githubAPI: gh.URL, githubToken: "t", repo: "o/r"})
	c.kube = newKubeAgainst(kube.URL)
	raw, err := c.readHealthReport(context.Background())
	if err != nil {
		t.Fatalf("ConfigMap から読めるはず: %v", err)
	}
	if !strings.Contains(string(raw), `"immich"`) {
		t.Fatalf("レポートをそのまま返すべき: %q", raw)
	}
}

func TestDriverFallsBackToTheBranch(t *testing.T) {
	kube := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer kube.Close()
	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(healthReportJSON))
	}))
	defer gh.Close()

	c := newClient(&config{githubAPI: gh.URL, githubToken: "t", repo: "o/r"})
	c.kube = newKubeAgainst(kube.URL)
	if _, err := c.readHealthReport(context.Background()); err != nil {
		t.Fatalf("ブランチに落ちて読めるはず: %v", err)
	}
}

// quoteJSON は文字列を JSON の文字列リテラルにする (テストの ConfigMap 組み立て用)。
func quoteJSON(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, `\"`) + `"`
}

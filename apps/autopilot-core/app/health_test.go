// 健全性の見張りの契約を固定する。
//
// 守りたいのは「同じ異常で 30 分ごとに騒がない」ことと、
// 「読めなかったことを異常の不在として扱わない」こと。
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

// configMapWith は reporter が書く ConfigMap の応答を組み立てる
// (data.latest.json にレポートの JSON 文字列が入る形)。
func configMapWith(report string) []byte {
	body, err := json.Marshal(map[string]any{"data": map[string]string{"latest.json": report}})
	if err != nil {
		panic(err)
	}
	return body
}

func docWith(apps ...[3]string) healthDoc {
	var d healthDoc
	d.GeneratedAt = "2026-08-23T13:30:09Z"
	for _, a := range apps {
		d.Applications = append(d.Applications, struct {
			Name   string `json:"name"`
			Sync   string `json:"sync"`
			Health string `json:"health"`
		}{Name: a[0], Sync: a[1], Health: a[2]})
	}
	return d
}

func TestUnhealthyAppsPicksNonGreenOnly(t *testing.T) {
	d := docWith(
		[3]string{"immich", "Synced", "Degraded"},
		[3]string{"coder", "Synced", "Healthy"},
		[3]string{"argocd", "OutOfSync", "Healthy"},
	)
	got := unhealthyApps(d)
	want := []string{"argocd(OutOfSync/Healthy)", "immich(Synced/Degraded)"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("got %v, want %v (名前順)", got, want)
	}
}

func TestUnhealthyAppsEmptyWhenAllGreen(t *testing.T) {
	if got := unhealthyApps(docWith([3]string{"coder", "Synced", "Healthy"})); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

func TestHealthChangedOnlyOnDifference(t *testing.T) {
	if healthChanged([]string{"a"}, []string{"a"}) {
		t.Fatal("同じ顔ぶれなら起こさない (30 分ごとに同じ不満を言わせない)")
	}
	if !healthChanged([]string{"a"}, []string{"a", "b"}) {
		t.Fatal("増えたら起こす")
	}
	if !healthChanged([]string{"a"}, []string{}) {
		t.Fatal("復旧も変化として起こす")
	}
	if !healthChanged([]string{"a"}, []string{"b"}) {
		t.Fatal("顔ぶれが入れ替わったら起こす")
	}
}

func TestBuildHealthPromptSaysItIsSelfInitiated(t *testing.T) {
	d := docWith([3]string{"immich", "Synced", "Degraded"})
	got := buildHealthPrompt(d, []string{}, unhealthyApps(d))

	if !strings.Contains(got, "人間からの依頼ではなく") {
		t.Fatalf("自発的な気づきだと伝えるべき: %q", got)
	}
	if !strings.Contains(got, "直せるとは言わない") {
		t.Fatalf("修理できないことを釘刺すべき: %q", got)
	}
	if !strings.Contains(got, "immich(Synced/Degraded)") {
		t.Fatalf("何が不調かを伝えるべき: %q", got)
	}
}

func TestBuildHealthPromptOnRecovery(t *testing.T) {
	d := docWith([3]string{"immich", "Synced", "Healthy"})
	got := buildHealthPrompt(d, []string{"immich(Synced/Degraded)"}, []string{})
	if !strings.Contains(got, "復旧") {
		t.Fatalf("復旧として伝えるべき: %q", got)
	}
}

func TestWatchHealthFirstRunRecordsWithoutWaking(t *testing.T) {
	// 起動しただけで「変化した」と言わない
	var prompted bool
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		prompted = true
		w.WriteHeader(http.StatusNoContent)
	}))
	defer oc.Close()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(configMapWith(`{"generated_at":"t","applications":[{"name":"immich","sync":"Synced","health":"Degraded"}]}`))
	}))
	defer api.Close()

	dir := t.TempDir()
	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	cursor := filepath.Join(dir, "health-cursor.json")

	if c.watchHealth(context.Background(), "ses_1", cursor) {
		t.Fatal("初回は起こさない")
	}
	if prompted {
		t.Fatal("初回に prompt を投げてはいけない")
	}

	// 2 回目は同じ顔ぶれなので、やはり起こさない
	if c.watchHealth(context.Background(), "ses_1", cursor) {
		t.Fatal("同じ顔ぶれなら起こさない")
	}
}

func TestWatchHealthWakesOnChange(t *testing.T) {
	var body string
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		buf := make([]byte, 4096)
		n, _ := r.Body.Read(buf)
		body = string(buf[:n])
		w.WriteHeader(http.StatusNoContent)
	}))
	defer oc.Close()

	degraded := true
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		if degraded {
			_, _ = w.Write(configMapWith(`{"generated_at":"t","applications":[{"name":"immich","sync":"Synced","health":"Degraded"}]}`))
			return
		}
		_, _ = w.Write(configMapWith(`{"generated_at":"t","applications":[{"name":"immich","sync":"Synced","health":"Healthy"}]}`))
	}))
	defer api.Close()

	dir := t.TempDir()
	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	cursor := filepath.Join(dir, "health-cursor.json")

	c.watchHealth(context.Background(), "ses_1", cursor) // 初回: 記録のみ
	degraded = false
	if !c.watchHealth(context.Background(), "ses_1", cursor) {
		t.Fatal("復旧は変化なので起こすべき")
	}
	if !strings.Contains(body, "復旧") {
		t.Fatalf("復旧として渡すべき: %q", body)
	}
}

func TestWatchHealthStaysQuietWhenReportUnreadable(t *testing.T) {
	// 読めないことは「異常が無い」ではない。だが騒ぎもしない
	// (レポート未生成の間ずっと鳴り続けるのを避ける)
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("prompt を投げてはいけない")
	}))
	defer oc.Close()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	}))
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	if c.watchHealth(context.Background(), "ses_1", filepath.Join(t.TempDir(), "h.json")) {
		t.Fatal("読めないときは起こさない")
	}
}

// reporter (apps/ops-health-reporter/report.py) が書く場所と、コアが読む場所が
// 同じであることを固定する。ここがずれると「いつまでも変化に気づかない」形で壊れる。
func TestWatchHealthReadsReporterConfigMap(t *testing.T) {
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer oc.Close()

	var seen string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.URL.Path
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(configMapWith(`{"generated_at":"t","applications":[]}`))
	}))
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	c.watchHealth(context.Background(), "ses_1", filepath.Join(t.TempDir(), "h.json"))

	if seen != "/api/v1/namespaces/autopilot/configmaps/ops-health-report" {
		t.Fatalf("reporter の書き先と揃っていない: %q", seen)
	}
}

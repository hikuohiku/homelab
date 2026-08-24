// 沈黙の見張りが固定する契約 (設計 state-out-of-git Phase 7)。
//
//  1. 見るのは**ビートの鮮度**であってプロセスの生死ではない
//     (「プロセスは生きているがビートが止まっている」を検知できる)
//  2. **fail-closed** — 読めない / 壊れている / 時刻が無いは沈黙とみなす
//  3. 閾値は ops/rules.json が単一情報源。コードに埋めない
//  4. 同じ沈黙で人間を叩き続けない (cooldown)。顔ぶれが変わったら待たせない
package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func at(s string) time.Time {
	ts, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return ts
}

// --- 閾値の単一情報源 ---

func writeRules(t *testing.T, heartbeat, health int) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "ops"), 0o755); err != nil {
		t.Fatal(err)
	}
	doc := map[string]any{
		"heartbeat": map[string]any{"stale_seconds": heartbeat},
		"health":    map[string]any{"stale_seconds": health},
	}
	raw, _ := json.Marshal(doc)
	if err := os.WriteFile(filepath.Join(dir, "ops", "rules.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestLoadThresholdsReadsRulesJSON(t *testing.T) {
	got, err := loadThresholds(writeRules(t, 7200, 21600))
	if err != nil {
		t.Fatalf("読めない: %v", err)
	}
	if got.heartbeat != 2*time.Hour || got.health != 6*time.Hour {
		t.Fatalf("閾値が rules.json と違う: %+v", got)
	}
}

func TestLoadThresholdsFailsWithoutRules(t *testing.T) {
	if _, err := loadThresholds(t.TempDir()); err == nil {
		t.Fatal("rules.json が無いのにエラーにならない。既定値を勝手に決めてはいけない")
	}
}

func TestLoadThresholdsFailsOnMissingKey(t *testing.T) {
	dir := t.TempDir()
	_ = os.MkdirAll(filepath.Join(dir, "ops"), 0o755)
	_ = os.WriteFile(filepath.Join(dir, "ops", "rules.json"), []byte(`{"heartbeat":{}}`), 0o644)
	if _, err := loadThresholds(dir); err == nil {
		t.Fatal("stale_seconds が無いのにエラーにならない")
	}
}

// --- 鮮度の判定 ---

// この試験がこの仕組みの存在理由そのもの。P-0027 の事故は「止まったまま死んだ」で、
// プロセスは生きていた。renewTime は beat() が最後まで通ったときだけ進むので、
// 「到達はできるが値が古い」が沈黙として出ることを固定する。
func TestStuckButAliveIsSilence(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	// 読み取りは成功している (= プロセスも API も生きている)。だが 3 時間古い
	f := judgeFreshness("heart", "heart のビート", "2026-08-25T09:00:00Z", nil, now, 2*time.Hour)
	if !f.stale {
		t.Fatal("ビートが 3 時間止まっているのに沈黙と判定していない")
	}
	if !strings.Contains(f.reason, "更新されていない") {
		t.Fatalf("理由が沈黙を説明していない: %s", f.reason)
	}
}

func TestFreshIsNotSilence(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	f := judgeFreshness("heart", "heart のビート", "2026-08-25T11:58:00Z", nil, now, 2*time.Hour)
	if f.stale {
		t.Fatalf("2 分前の更新を沈黙と判定した: %s", f.reason)
	}
}

func TestFailClosed(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	cases := []struct {
		name string
		at   string
		err  error
	}{
		{"読めない", "", errors.New("k8s API に拒否された (status=403)")},
		{"時刻が無い", "", nil},
		{"時刻が壊れている", "きのう", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := judgeFreshness("heart", "heart のビート", tc.at, tc.err, now, 2*time.Hour)
			if !f.stale {
				t.Fatalf("%s を fresh に倒した。読めないことは元気の証拠ではない", tc.name)
			}
		})
	}
}

func TestClockSkewIsNotSilence(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	f := judgeFreshness("heart", "heart のビート", "2026-08-25T12:05:00Z", nil, now, 2*time.Hour)
	if f.stale {
		t.Fatal("未来の時刻を沈黙と判定した (clock skew は沈黙ではない)")
	}
	if !strings.Contains(f.reason, "clock skew") {
		t.Fatalf("skew を理由に残していない: %s", f.reason)
	}
}

func TestBoundaryIsStale(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	f := judgeFreshness("heart", "heart のビート", "2026-08-25T10:00:00Z", nil, now, 2*time.Hour)
	if !f.stale {
		t.Fatal("閾値ちょうどは沈黙側に倒すこと")
	}
}

func TestParseStampAcceptsKubeMicroTime(t *testing.T) {
	// k8s の MicroTime は小数秒を付けて返しうる
	for _, s := range []string{"2026-08-25T12:00:00Z", "2026-08-25T12:00:00.123456Z"} {
		if _, err := parseStamp(s); err != nil {
			t.Fatalf("%q を読めない: %v", s, err)
		}
	}
}

// --- 通知するかどうか ---

func TestAlertsOnNewSilence(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{"heart"}, []string{}, time.Time{}, now, silenceRepostCooldown)
	if !ok {
		t.Fatal("新しい沈黙を黙って見過ごした")
	}
}

func TestSuppressesRepeatWithinCooldown(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{"heart"}, []string{"heart"}, now.Add(-time.Hour), now, silenceRepostCooldown)
	if ok {
		t.Fatal("同じ沈黙を 1 時間で言い直した")
	}
}

func TestRepeatsAfterCooldown(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{"heart"}, []string{"heart"}, now.Add(-7*time.Hour), now, silenceRepostCooldown)
	if !ok {
		t.Fatal("cooldown を過ぎても言い直さない")
	}
}

func TestAlertsWhenSetChangesEvenInCooldown(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{"health-report", "heart"}, []string{"heart"},
		now.Add(-time.Minute), now, silenceRepostCooldown)
	if !ok {
		t.Fatal("沈黙が増えたのに cooldown で黙った")
	}
}

func TestAlertsOnRecovery(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{}, []string{"heart"}, now.Add(-time.Minute), now, silenceRepostCooldown)
	if !ok {
		t.Fatal("回復を知らせない")
	}
}

func TestQuietWhenNothingIsWrong(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	ok, _ := shouldAlert([]string{}, []string{}, time.Time{}, now, silenceRepostCooldown)
	if ok {
		t.Fatal("何も起きていないのに話しかけた")
	}
}

// --- コアへの話しかけ ---

func TestPromptTellsCoreNotToClaimARepair(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	f := judgeFreshness("heart", "heart のビート (Lease の renewTime)",
		"2026-08-25T08:00:00Z", nil, now, 2*time.Hour)
	got := buildSilencePrompt([]finding{f}, []string{"heart"}, []string{}, now)
	for _, want := range []string{"telegram_reply", "直せるとは言わない", "heart のビート"} {
		if !strings.Contains(got, want) {
			t.Fatalf("prompt に %q が無い:\n%s", want, got)
		}
	}
}

func TestRecoveryPromptIsShort(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	got := buildSilencePrompt(nil, []string{}, []string{"heart"}, now)
	if !strings.Contains(got, "解消") {
		t.Fatalf("回復の prompt になっていない:\n%s", got)
	}
}

// --- 見張りの周回 ---

func TestTickHoldsBackWithoutThresholds(t *testing.T) {
	w := &silenceWatcher{cursorPath: filepath.Join(t.TempDir(), "c.json"), repoDir: t.TempDir()}
	if w.tick(t.Context(), &client{}, "s1", time.Now()) {
		t.Fatal("閾値が読めないのに判定した。埋め込みの既定で走ってはいけない")
	}
}

func TestCursorRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "silence-cursor.json")
	if got := loadSilenceCursor(path); len(got.Stale) != 0 || got.AlertedAt != "" {
		t.Fatalf("無い cursor が空で返らない: %+v", got)
	}
	if err := writeJSON(path, silenceCursor{Stale: []string{"heart"}, AlertedAt: "2026-08-25T12:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	got := loadSilenceCursor(path)
	if len(got.Stale) != 1 || got.Stale[0] != "heart" || got.AlertedAt != "2026-08-25T12:00:00Z" {
		t.Fatalf("読み戻せない: %+v", got)
	}
}

// --- 実経路 (k8s から読んで、コアに話しかけるまで) ---

// leaseAndHealth は Lease と健全性 ConfigMap を返す偽の API server。
func leaseAndHealth(t *testing.T, renewTime, generatedAt string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		if strings.Contains(r.URL.Path, "/leases/") {
			_, _ = w.Write([]byte(`{"spec":{"renewTime":"` + renewTime + `"}}`))
			return
		}
		_, _ = w.Write(configMapWith(`{"generated_at":"` + generatedAt + `","applications":[]}`))
	}))
}

func newWatcher(t *testing.T, dir string) *silenceWatcher {
	t.Helper()
	return &silenceWatcher{
		cursorPath: filepath.Join(t.TempDir(), "silence-cursor.json"),
		repoDir:    dir,
	}
}

// heart だけが黙っている (プロセスも API も生きていて、値だけが古い) 場合。
func TestTickAlertsOnStaleLeaseThenGoesQuiet(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc, texts, prompts := promptRecorder(t, http.StatusNoContent)
	api := leaseAndHealth(t, "2026-08-25T08:00:00Z", "2026-08-25T11:50:00Z")
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	w := newWatcher(t, writeRules(t, 7200, 21600))

	if !w.tick(t.Context(), c, "ses_1", now) {
		t.Fatal("4 時間前のビートを沈黙として通知していない")
	}
	if len(*texts) != 1 || !strings.Contains((*texts)[0], "heart") {
		t.Fatalf("何が黙っているかを渡していない: %v", *texts)
	}
	// 同じ沈黙が続く間は cooldown で黙る
	if w.tick(t.Context(), c, "ses_1", now.Add(time.Minute)) {
		t.Fatal("同じ沈黙を 1 分後に言い直した")
	}
	if *prompts != 1 {
		t.Fatalf("prompt が %d 回。1 回のはず", *prompts)
	}
}

// reporter 自身の死 (Phase 5 以降、誰も見張っていなかった穴)。
func TestTickAlertsOnStaleHealthReport(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc, texts, _ := promptRecorder(t, http.StatusNoContent)
	// heart は元気、レポートだけ 7 時間古い
	api := leaseAndHealth(t, "2026-08-25T11:59:00Z", "2026-08-25T05:00:00Z")
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)

	if !newWatcher(t, writeRules(t, 7200, 21600)).tick(t.Context(), c, "ses_1", now) {
		t.Fatal("健全性レポートの停止を検知していない")
	}
	if len(*texts) != 1 || !strings.Contains((*texts)[0], "健全性レポート") {
		t.Fatalf("レポートの停止として渡していない: %v", *texts)
	}
}

func TestTickStaysQuietWhenEverythingIsFresh(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("何も起きていないのに prompt を投げた")
	}))
	defer oc.Close()
	api := leaseAndHealth(t, "2026-08-25T11:59:00Z", "2026-08-25T11:40:00Z")
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	if newWatcher(t, writeRules(t, 7200, 21600)).tick(t.Context(), c, "ses_1", now) {
		t.Fatal("全部新しいのに通知した")
	}
}

// k8s に届かないことも沈黙 (fail-closed)。ここを「読めないので判定しない」に
// すると、権限を失った瞬間から永久に黙る見張りになる。
func TestTickTreatsUnreadableAsSilence(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc, texts, _ := promptRecorder(t, http.StatusNoContent)
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	if !newWatcher(t, writeRules(t, 7200, 21600)).tick(t.Context(), c, "ses_1", now) {
		t.Fatal("403 を沈黙として扱っていない (fail-closed)")
	}
	if len(*texts) != 1 || !strings.Contains((*texts)[0], "heart") ||
		!strings.Contains((*texts)[0], "健全性レポート") {
		t.Fatalf("両方の沈黙を渡すべき: %v", *texts)
	}
}

// heart の Lease を読む先が、heart の書き先 (ops/heart/lease.py の NAME) と揃っていること。
func TestTickReadsTheHeartLease(t *testing.T) {
	var seen string
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer oc.Close()
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/leases/") {
			seen = r.URL.Path
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"spec":{"renewTime":"2026-08-25T11:59:00Z"}}`))
	}))
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	newWatcher(t, writeRules(t, 7200, 21600)).tick(t.Context(), c, "ses_1", at("2026-08-25T12:00:00Z"))

	want := "/apis/coordination.k8s.io/v1/namespaces/autopilot/leases/autopilot-heart"
	if seen != want {
		t.Fatalf("Lease の読み先が %q。heart の書き先と揃えること (%q)", seen, want)
	}
}

// 回復したら cursor が空に戻り、次の沈黙をまた待たせずに言える。
func TestTickRecoveryClearsCursor(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer oc.Close()
	stale := true
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		if strings.Contains(r.URL.Path, "/leases/") {
			if stale {
				_, _ = w.Write([]byte(`{"spec":{"renewTime":"2026-08-25T06:00:00Z"}}`))
			} else {
				_, _ = w.Write([]byte(`{"spec":{"renewTime":"2026-08-25T11:59:00Z"}}`))
			}
			return
		}
		_, _ = w.Write(configMapWith(`{"generated_at":"2026-08-25T11:50:00Z","applications":[]}`))
	}))
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	w := newWatcher(t, writeRules(t, 7200, 21600))

	w.tick(t.Context(), c, "ses_1", now)
	stale = false
	if !w.tick(t.Context(), c, "ses_1", now.Add(time.Minute)) {
		t.Fatal("回復を知らせていない")
	}
	if got := loadSilenceCursor(w.cursorPath); len(got.Stale) != 0 {
		t.Fatalf("回復後も沈黙が残っている: %+v", got)
	}
}

// 言えなかったら cursor を進めない (次の周回でやり直す)。
func TestTickDoesNotAdvanceCursorWhenPromptFails(t *testing.T) {
	now := at("2026-08-25T12:00:00Z")
	oc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer oc.Close()
	api := leaseAndHealth(t, "2026-08-25T06:00:00Z", "2026-08-25T11:50:00Z")
	defer api.Close()

	c := newClient(&config{opencodeURL: oc.URL})
	c.kube = newKubeAgainst(api.URL)
	w := newWatcher(t, writeRules(t, 7200, 21600))

	if w.tick(t.Context(), c, "ses_1", now) {
		t.Fatal("渡せていないのに成功として返した")
	}
	if _, err := os.Stat(w.cursorPath); err == nil {
		t.Fatal("渡せていないのに cursor を進めた")
	}
}

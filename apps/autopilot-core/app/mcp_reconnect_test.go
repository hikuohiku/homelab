// MCP サイドカーの再接続を固定する。
//
// 一番守りたいのは「opencode が connected と言っていても、サイドカーが入れ替わって
// いれば繋ぎ直す」こと。ここが緩むとツールが黙って壊れたままになる。
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

func TestParseMCPTargets(t *testing.T) {
	got, err := parseMCPTargets("telegram=http://127.0.0.1:4097, homelab=http://127.0.0.1:4098/")
	if err != nil {
		t.Fatal(err)
	}
	want := []mcpTarget{
		{name: "telegram", base: "http://127.0.0.1:4097"},
		{name: "homelab", base: "http://127.0.0.1:4098"},
	}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %+v, want %+v", got, want)
		}
	}

	if got, err := parseMCPTargets(""); err != nil || len(got) != 0 {
		t.Fatalf("空は見張り無し: %+v %v", got, err)
	}
	if _, err := parseMCPTargets("telegram"); err == nil {
		t.Fatal("name=url 形式でない項は拒否すべき")
	}
}

func TestNeedsReconnect(t *testing.T) {
	for _, tc := range []struct {
		name               string
		last, boot, status string
		want               bool
	}{
		{name: "起動直後 (まだ繋いでいない)", last: "", boot: "b1", status: "connected", want: true},
		{name: "何も変わっていない", last: "b1", boot: "b1", status: "connected", want: false},
		{name: "サイドカーが入れ替わった (opencode は connected のまま)", last: "b1", boot: "b2", status: "connected", want: true},
		{name: "opencode 側が failed", last: "b1", boot: "b1", status: "failed", want: true},
		{name: "opencode の状態が読めない", last: "b1", boot: "b1", status: "", want: true},
		{name: "サイドカーが応じない", last: "b1", boot: "", status: "failed", want: false},
	} {
		if got := needsReconnect(tc.last, tc.boot, tc.status); got != tc.want {
			t.Fatalf("%s: got %v, want %v", tc.name, got, tc.want)
		}
	}
}

// fakeSidecar は boot を差し替えられる /healthz。
type fakeSidecar struct {
	mu   sync.Mutex
	boot string
}

func (f *fakeSidecar) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	_ = json.NewEncoder(w).Encode(map[string]string{"boot": f.boot})
}

func (f *fakeSidecar) restart(boot string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.boot = boot
}

func TestWatcherReconnectsAfterSidecarRestart(t *testing.T) {
	sidecar := &fakeSidecar{boot: "b1"}
	sidecarSrv := httptest.NewServer(sidecar)
	defer sidecarSrv.Close()

	var connects []string
	opencode := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			connects = append(connects, r.URL.Path)
			_, _ = w.Write([]byte("true"))
			return
		}
		// opencode は壊れていても connected と言い続ける
		_, _ = w.Write([]byte(`{"homelab":{"status":"connected"}}`))
	}))
	defer opencode.Close()

	c := newClient(&config{opencodeURL: opencode.URL})
	w := &mcpWatcher{c: c, boots: map[string]string{},
		targets: []mcpTarget{{name: "homelab", base: sidecarSrv.URL}}}
	ctx := context.Background()

	w.sync(ctx) // 起動直後は 1 回繋ぐ
	w.sync(ctx) // 何も変わっていなければ繋ぎ直さない
	if len(connects) != 1 || connects[0] != "/mcp/homelab/connect" {
		t.Fatalf("起動時に 1 回だけ繋ぐべき: %+v", connects)
	}

	sidecar.restart("b2")
	w.sync(ctx)
	if len(connects) != 2 {
		t.Fatalf("サイドカーが入れ替わったら繋ぎ直すべき (connected のままでも): %+v", connects)
	}
}

func TestWatcherLeavesDeadSidecarAlone(t *testing.T) {
	opencode := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			t.Error("応じないサイドカーを繋ぎ直しても無駄")
		}
		_, _ = w.Write([]byte(`{"homelab":{"status":"failed"}}`))
	}))
	defer opencode.Close()

	w := &mcpWatcher{c: newClient(&config{opencodeURL: opencode.URL}), boots: map[string]string{},
		targets: []mcpTarget{{name: "homelab", base: "http://127.0.0.1:1"}}}
	w.sync(context.Background())
}

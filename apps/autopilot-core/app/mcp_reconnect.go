// MCP サイドカーの再接続。driver が担う。
//
// なぜ要るか: opencode は remote MCP の接続状態を自動では更新せず、自動再接続も
// しない (2026-08-24 実測)。MCP サイドカーが再起動すると、opencode 側は
// `connected` のままなのにツールは黙って壊れる。人間から見ると「コアが急に
// homelab を見られなくなった」だけで、どこにもエラーが出ない。
//
// 直し方は `POST /mcp/<name>/connect` (opencode 本体の再起動は不要)。
// 誰がそれを叩くかというと、既に opencode の HTTP API を叩いている driver が素直。
//
// 「再起動したか」は opencode 側の status では分からない (壊れていても connected の
// まま) ので、サイドカー自身の `/healthz` が返す boot 識別子を見る。値が変われば
// プロセスが入れ替わったということ。driver 自身の再起動時は boot を覚えていないので
// 1 回繋ぎ直す (冪等なので実害は無く、起動順の取りこぼしをここで吸収できる)。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// mcpTarget は opencode.json の mcp キーと、そのサイドカーの待ち受け先の対。
type mcpTarget struct {
	name string
	base string
}

// parseMCPTargets は "telegram=http://127.0.0.1:4097,homelab=http://127.0.0.1:4098"
// を読む。空なら再接続の見張りをしない (切り戻し構成)。
func parseMCPTargets(spec string) ([]mcpTarget, error) {
	out := []mcpTarget{}
	for _, item := range strings.Split(spec, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		name, base, ok := strings.Cut(item, "=")
		name, base = strings.TrimSpace(name), strings.TrimSuffix(strings.TrimSpace(base), "/")
		if !ok || name == "" || base == "" {
			return nil, fmt.Errorf("CORE_MCP_TARGETS の項が name=url 形式でない: %q", item)
		}
		out = append(out, mcpTarget{name: name, base: base})
	}
	return out, nil
}

// needsReconnect は「繋ぎ直すべきか」を返す。
//
//	lastBoot — 最後に繋ぎ直したときのサイドカーの boot ("" なら未接続扱い)
//	boot     — いまのサイドカーの boot
//	status   — opencode が持っている接続状態 ("" なら分からなかった)
//
// status が connected でも boot が変わっていれば繋ぎ直す。これが本題で、
// opencode の connected は「壊れていない」を意味しない。
func needsReconnect(lastBoot, boot, status string) bool {
	if boot == "" {
		return false // サイドカーが応じない。繋ぎ直しても無駄
	}
	if lastBoot != boot {
		return true
	}
	return status != "connected"
}

type mcpWatcher struct {
	c       *client
	targets []mcpTarget
	boots   map[string]string
}

func newMCPWatcher(c *client) (*mcpWatcher, error) {
	targets, err := parseMCPTargets(envOr("CORE_MCP_TARGETS",
		"telegram=http://127.0.0.1:4097,homelab=http://127.0.0.1:4098"))
	if err != nil {
		return nil, err
	}
	return &mcpWatcher{c: c, targets: targets, boots: map[string]string{}}, nil
}

// probe はサイドカーの boot を返す。応じなければ "".
func (w *mcpWatcher) probe(ctx context.Context, t mcpTarget) string {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, t.base+"/healthz", nil)
	if err != nil {
		return ""
	}
	resp, err := w.c.http.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	var parsed struct {
		Boot string `json:"boot"`
	}
	if json.NewDecoder(resp.Body).Decode(&parsed) != nil {
		return ""
	}
	return parsed.Boot
}

// statuses は opencode が持っている MCP の接続状態を返す。
// 読めなければ空 map (= 分からない) で、その場合は boot の変化だけで判断する。
func (w *mcpWatcher) statuses(ctx context.Context) map[string]string {
	code, raw, err := w.c.opencode(ctx, http.MethodGet, "/mcp", nil)
	if err != nil || code != http.StatusOK {
		return map[string]string{}
	}
	var parsed map[string]struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(raw, &parsed) != nil {
		return map[string]string{}
	}
	out := map[string]string{}
	for name, v := range parsed {
		out[name] = v.Status
	}
	return out
}

// sync は必要なぶんだけ繋ぎ直す。
func (w *mcpWatcher) sync(ctx context.Context) {
	if len(w.targets) == 0 {
		return
	}
	status := w.statuses(ctx)
	for _, t := range w.targets {
		boot := w.probe(ctx, t)
		if !needsReconnect(w.boots[t.name], boot, status[t.name]) {
			continue
		}
		code, raw, err := w.c.opencode(ctx, http.MethodPost, "/mcp/"+t.name+"/connect", nil)
		if err != nil || code != http.StatusOK {
			log.Printf("MCP %s を繋ぎ直せない (次の周回で再試行): status=%d %v %s",
				t.name, code, err, truncate(string(raw), 200))
			continue
		}
		w.boots[t.name] = boot
		before := status[t.name]
		if before == "" {
			before = "(不明)"
		}
		log.Printf("MCP %s を繋ぎ直した (boot=%s, 直前の opencode 側の状態=%s)", t.name, boot, before)
	}
}

// mcpCheckInterval は見張りの間隔。壊れている間コアのツールが黙って死んでいるので
// 短めに見るが、ローカルへの GET が 2 本増えるだけなので負荷は無視できる。
func mcpCheckInterval() time.Duration {
	return time.Duration(envOrInt("CORE_MCP_CHECK_SECONDS", 30)) * time.Second
}

// MCP stdio サーバ。コアに「homelab を見る目」を与える。
//
//	homelab_status — autopilot 自身の状態 (走行中エージェント / プロジェクト / 要対応 / 心拍 / 当日消費)
//	homelab_health — ArgoCD Application / Pod / PVC / Node の健全性
//
// どちらも引数を取らない読み取り専用。汎用の HTTP fetch や kubectl を与えるのではなく
// 用途を固定した窓を 2 つ開けるだけにしてあるのは、コアが到達できる先を設定ではなく
// コードで縛るため。新しい credential も RBAC も要らない:
//
//   - status はクラスタ内の ops-dashboard (認証不要・read-only の API)
//   - health は driver が既に持つ GitHub トークンで ops-health-report ブランチを読む
//
// 返すのは取得した JSON そのまま。要約はコアの仕事で、ここでは加工しない
// (加工すると「取れなかった」と「空だった」の区別が消える)。
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// 1 ツール応答の上限。dashboard の snapshot が育ってもコアの文脈を食い潰さない。
const maxToolResultBytes = 20000

const (
	mcpDefaultProtocol = "2025-06-18"
	mcpServerName      = "autopilot-core"
	mcpServerVersion   = "1"
)

// --- JSON-RPC 2.0 ---

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type toolDef struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	InputSchema any    `json:"inputSchema"`
}

type textContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type toolResult struct {
	Content []textContent `json:"content"`
	IsError bool          `json:"isError,omitempty"`
}

// noArgsSchema は「引数を取らない」ことを明示する schema。
func noArgsSchema() any {
	return map[string]any{"type": "object", "properties": map[string]any{}}
}

func toolDefs() []toolDef {
	return []toolDef{
		{
			Name: "homelab_status",
			Description: "autopilot 自身のいまの状態を返す。走行中のエージェント、プロジェクトの状態機械、" +
				"要対応 (attention)、最後の心拍、当日の消費。引数は取らない。" +
				"数字を答えるときは必ずこれを呼ぶこと。取れなければ取れないと言う。",
			InputSchema: noArgsSchema(),
		},
		{
			Name: "homelab_health",
			Description: "homelab の健全性を返す。ArgoCD Application の sync/health、Pod、PVC、Node。" +
				"引数は取らない。30 分ごとに更新されるレポートなので、generated_at が古ければ " +
				"その旨を添えて答えること。",
			InputSchema: noArgsSchema(),
		},
	}
}

// --- 取得 ---

func (c *client) fetchStatus(ctx context.Context) (string, error) {
	url := envOr("CORE_DASHBOARD_URL", "http://ops-dashboard.autopilot.svc") + "/api/snapshot"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("ops-dashboard に届かない: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("ops-dashboard が %d を返した: %s", resp.StatusCode, truncate(string(raw), 200))
	}
	return clip(string(raw)), nil
}

func (c *client) fetchHealth(ctx context.Context) (string, error) {
	branch := envOr("CORE_HEALTH_BRANCH", "ops-health-report")
	path := envOr("CORE_HEALTH_PATH", "ops/health/latest.json")
	status, raw, err := c.github(ctx,
		fmt.Sprintf("/repos/%s/contents/%s?ref=%s", c.cfg.repo, path, branch),
		"application/vnd.github.raw")
	if err != nil {
		return "", err
	}
	if status != http.StatusOK {
		return "", fmt.Errorf("health レポートを読めない (status=%d)", status)
	}
	return clip(string(raw)), nil
}

func clip(s string) string {
	if len(s) <= maxToolResultBytes {
		return s
	}
	return s[:maxToolResultBytes] + "\n… (長いので切り詰めた)"
}

// --- サーバ ---

type mcpServer struct {
	client *client
	out    *json.Encoder
}

func (s *mcpServer) respond(id json.RawMessage, result any) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Result: result})
}

func (s *mcpServer) respondError(id json.RawMessage, code int, message string) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: code, Message: message}})
}

func (s *mcpServer) callTool(ctx context.Context, name string) toolResult {
	var (
		body string
		err  error
	)
	switch name {
	case "homelab_status":
		body, err = s.client.fetchStatus(ctx)
	case "homelab_health":
		body, err = s.client.fetchHealth(ctx)
	default:
		return toolResult{Content: []textContent{{Type: "text", Text: "未知のツール: " + name}}, IsError: true}
	}
	if err != nil {
		// 取得できないことを isError で返す。ここを握り潰すと、コアが
		// 「取れなかった」を「異常なし」と取り違える
		return toolResult{Content: []textContent{{Type: "text", Text: "取得できなかった: " + err.Error()}}, IsError: true}
	}
	return toolResult{Content: []textContent{{Type: "text", Text: body}}}
}

func (s *mcpServer) handle(ctx context.Context, req rpcRequest) {
	isNotification := len(req.ID) == 0 || string(req.ID) == "null"

	switch req.Method {
	case "initialize":
		var p struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &p)
		version := p.ProtocolVersion
		if version == "" {
			version = mcpDefaultProtocol
		}
		s.respond(req.ID, map[string]any{
			"protocolVersion": version,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      map[string]any{"name": mcpServerName, "version": mcpServerVersion},
		})

	case "tools/list":
		s.respond(req.ID, map[string]any{"tools": toolDefs()})

	case "tools/call":
		var p struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			s.respondError(req.ID, -32602, "params を解釈できない: "+err.Error())
			return
		}
		s.respond(req.ID, s.callTool(ctx, p.Name))

	case "ping":
		s.respond(req.ID, map[string]any{})

	default:
		if isNotification {
			return
		}
		s.respondError(req.ID, -32601, "未実装のメソッド: "+req.Method)
	}
}

func (s *mcpServer) serve(ctx context.Context, in io.Reader) error {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 0, 64*1024), 8<<20)

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req rpcRequest
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			fmt.Fprintf(os.Stderr, "[core-driver/mcp] 解釈できない行を捨てた: %v\n", err)
			continue
		}
		s.handle(ctx, req)
	}
	return scanner.Err()
}

func runMCP() {
	log.SetFlags(0)
	log.SetPrefix("[core-driver/mcp] ")
	// stdout は JSON-RPC 専用。ログは必ず stderr へ
	log.SetOutput(os.Stderr)

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("起動できません: %v", err)
	}
	c := newClient(cfg)
	c.http.Timeout = 30 * time.Second

	server := &mcpServer{client: c, out: json.NewEncoder(os.Stdout)}
	if err := server.serve(context.Background(), os.Stdin); err != nil {
		log.Fatalf("stdin の読み取りに失敗: %v", err)
	}
}

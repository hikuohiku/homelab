// MCP stdio サーバ。常駐コアが人間へ直接返信するための口を 1 つだけ提供する。
//
//	telegram_reply(text) — allowlist の人間へ Telegram DM を送る
//
// 受信 (adapter モード) と同じバイナリに置くのは、allowlist の解釈と Telegram の
// 呼び出し方を一箇所に保つため。受信と送信で判定がずれると、拾わない相手に喋る
// といった事故になる。
//
// 安全側の設計:
//
//   - 宛先を引数に取らない。送り先は常に TELEGRAM_ALLOWED_USER_ID で、コアが
//     任意の chat_id を指定する余地を持たない。プロンプト注入で「別の相手に送れ」と
//     指示されても到達先が変わらない。
//   - allowlist 未設定なら初期化に失敗する (fail-closed)。受信側と同じ向き。
//   - GitHub トークンを要求しない。このモードは inbox に触らない。
//
// 転送は 2 通り。既定は stdio で、`--listen host:port` を渡すと HTTP streamable に
// なる (mcp_http.go)。本番のコアは後者を使う — stdio だと opencode の子プロセスに
// なるため、Telegram のトークンを opencode 自身の env に置くことになり、
// bash を持つコアから読めてしまう。
//
//	{"mcp": {"telegram": {"type": "remote", "url": "http://127.0.0.1:4097/mcp",
//	  "enabled": true, "oauth": false}}}
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
)

// 送信 1 通の上限。Telegram の 4096 文字制限より手前で切って、
// 長文が黙って途中で消える (API 側 400) のを防ぐ。
const maxReplyRunes = 4000

const (
	mcpDefaultProtocol = "2025-06-18"
	mcpServerName      = "telegram-adapter"
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

// --- MCP のデータ形 ---

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

func replyToolDef() toolDef {
	return toolDef{
		Name: "telegram_reply",
		Description: "所有者へ Telegram の DM を送る。宛先は allowlist 済みの所有者に固定されており、" +
			"指定はできない。人間に届けたい返答・報告・確認をここに書く。",
		InputSchema: map[string]any{
			"type": "object",
			"properties": map[string]any{
				"text": map[string]any{
					"type":        "string",
					"description": "送る本文。Telegram の 1 通として読める長さにすること。",
				},
			},
			"required": []string{"text"},
		},
	}
}

// --- 送信 ---

// sendReply は allowlist の所有者へ 1 通送る。宛先は引数に取らない (設計上の固定)。
func (c *client) sendReply(ctx context.Context, text string) (int64, error) {
	if !c.cfg.hasAllowUser {
		return 0, errors.New("TELEGRAM_ALLOWED_USER_ID が未設定のため送信先が無い")
	}
	if strings.TrimSpace(text) == "" {
		return 0, errors.New("本文が空")
	}
	if n := len([]rune(text)); n > maxReplyRunes {
		return 0, fmt.Errorf("本文が長すぎる (%d 文字 > %d)。分割して送ること", n, maxReplyRunes)
	}

	params := url.Values{}
	params.Set("chat_id", strconv.FormatInt(c.cfg.allowedUser, 10))
	params.Set("text", text)

	raw, err := c.telegram(ctx, "sendMessage", params)
	if err != nil {
		return 0, err
	}
	var parsed struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int64 `json:"message_id"`
		} `json:"result"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return 0, err
	}
	if !parsed.OK {
		return 0, fmt.Errorf("telegram sendMessage: ok=false: %s", truncate(parsed.Description, 200))
	}
	return parsed.Result.MessageID, nil
}

// --- サーバ ---

type mcpServer struct {
	client *client
	// out は応答の書き出し先。stdio では stdout、HTTP では 1 リクエストごとの
	// バッファへ差し替える (mcp_http.go の handleBuffered)。mu はその差し替えの排他
	out *json.Encoder
	mu  sync.Mutex
}

func (s *mcpServer) respond(id json.RawMessage, result any) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Result: result})
}

func (s *mcpServer) respondError(id json.RawMessage, code int, message string) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: code, Message: message}})
}

// handle は 1 リクエストを処理する。id を持たない通知には応答しない (JSON-RPC の規約)。
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
		s.respond(req.ID, map[string]any{"tools": []toolDef{replyToolDef()}})

	case "tools/call":
		var p struct {
			Name      string `json:"name"`
			Arguments struct {
				Text string `json:"text"`
			} `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			s.respondError(req.ID, -32602, "params を解釈できない: "+err.Error())
			return
		}
		if p.Name != "telegram_reply" {
			s.respondError(req.ID, -32602, "未知のツール: "+p.Name)
			return
		}
		messageID, err := s.client.sendReply(ctx, p.Arguments.Text)
		if err != nil {
			// ツールの失敗はプロトコル誤りではないので、isError でモデルに返す
			// (そうしないとモデルが失敗を観測できず、黙って届いていないことになる)
			s.respond(req.ID, toolResult{
				Content: []textContent{{Type: "text", Text: "送信に失敗: " + err.Error()}},
				IsError: true,
			})
			return
		}
		s.respond(req.ID, toolResult{
			Content: []textContent{{Type: "text", Text: fmt.Sprintf("送信しました (message_id=%d)", messageID)}},
		})

	case "ping":
		s.respond(req.ID, map[string]any{})

	default:
		if isNotification {
			return // notifications/initialized など。黙って受け流す
		}
		s.respondError(req.ID, -32601, "未実装のメソッド: "+req.Method)
	}
}

func (s *mcpServer) serve(ctx context.Context, in io.Reader) error {
	scanner := bufio.NewScanner(in)
	// 1 行 1 メッセージ。長い本文が来ても切らないよう既定 64KB から広げる
	scanner.Buffer(make([]byte, 0, 64*1024), 8<<20)

	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req rpcRequest
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			// id が読めないので応答のしようがない。stderr に出して次へ
			fmt.Fprintf(os.Stderr, "[telegram-adapter/mcp] 解釈できない行を捨てた: %v\n", err)
			continue
		}
		s.handle(ctx, req)
	}
	return scanner.Err()
}

// runMCP は MCP サーバを 1 つ起動する。listen が空なら stdio、
// そうでなければ HTTP streamable でその addr を待ち受ける。
func runMCP(listen string) {
	log.SetFlags(0)
	log.SetPrefix("[telegram-adapter/mcp] ")
	// stdout は JSON-RPC 専用。ログは必ず stderr へ出す
	log.SetOutput(os.Stderr)

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("起動できません: %v", err)
	}
	if !cfg.hasAllowUser {
		log.Fatal("起動できません: TELEGRAM_ALLOWED_USER_ID が未設定/非数値です (送信先が定まらない)")
	}

	server := &mcpServer{client: newClient(cfg), out: json.NewEncoder(os.Stdout)}
	if listen != "" {
		log.SetFlags(log.LstdFlags | log.LUTC)
		if err := serveHTTPMCP(server, listen); err != nil {
			log.Fatalf("%v", err)
		}
		return
	}
	if err := server.serve(context.Background(), os.Stdin); err != nil {
		log.Fatalf("stdin の読み取りに失敗: %v", err)
	}
}

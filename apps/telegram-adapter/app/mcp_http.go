// MCP streamable HTTP 転送。stdio と同じ handle を、別プロセスから呼べる形で開く。
//
// なぜ要るか: opencode は local (stdio) の MCP を**子プロセス**として起動するため、
// MCP に渡したい秘密を opencode コンテナの env に置くしかなかった。コアに bash を
// 開けると /proc/self/environ からそれが全部読める。remote (HTTP) にしてサイドカーへ
// 移せば、秘密はコアのプロセスに存在しなくなる。
//
// 待ち受けは同一 Pod の loopback 限定 (--listen 127.0.0.1:PORT)。ネットワーク名前空間が
// 境界なので認証トークンは要らない。むしろ opencode の headers に秘密を置くと、
// GET /config が {env:...} 展開後の値をそのまま返すのでコアから丸見えになる。
//
// 応答は常に application/json の単発。SSE ストリームは開かない (要求/応答が
// 1 対 1 で、サーバ発の通知が無いため)。GET は 405 を返す。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// parseMCPListen は `mcp` サブコマンドの引数から待ち受け先を取り出す。
//
//	(なし)                       → stdio
//	--listen 127.0.0.1:4097      → HTTP streamable
//	--listen=127.0.0.1:4097      → 同上
//
// 未知の引数はエラーにする。黙って stdio で起動すると、待ち受けているつもりの
// サイドカーが誰にも繋がらないまま生き続ける。
func parseMCPListen(args []string) (string, error) {
	listen := ""
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case strings.HasPrefix(a, "--listen="):
			listen = strings.TrimPrefix(a, "--listen=")
		case a == "--listen":
			if i+1 >= len(args) {
				return "", fmt.Errorf("--listen にアドレスが無い")
			}
			i++
			listen = args[i]
		default:
			return "", fmt.Errorf("未知の引数: %s", a)
		}
	}
	if listen != "" && !strings.Contains(listen, ":") {
		return "", fmt.Errorf("--listen は host:port 形式で指定すること: %q", listen)
	}
	return listen, nil
}

// bootID はこのプロセスの識別子。opencode は MCP サーバが落ちても接続状態を
// connected のまま持ち続けるため、driver は「この値が変わったら再接続」で
// サイドカーの再起動を検出する。
var bootID = strconv.FormatInt(time.Now().UnixNano(), 36)

// 1 リクエストの上限。stdio 側の scanner buffer と揃える。
const maxHTTPRequestBytes = 8 << 20

// handleBuffered は handle の出力を横取りして 1 応答分のバイト列で返す。
// 通知 (id 無し) のときは空を返す。
//
// stdio 側の書き出し先 (s.out) を一時的に差し替える形にしてあるのは、
// 応答の組み立てを stdio と HTTP で 1 つに保つため。差し替えの間だけ排他する。
func (s *mcpServer) handleBuffered(ctx context.Context, req rpcRequest) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()

	var buf bytes.Buffer
	prev := s.out
	s.out = json.NewEncoder(&buf)
	s.handle(ctx, req)
	s.out = prev
	return bytes.TrimSpace(buf.Bytes())
}

func (s *mcpServer) httpMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/mcp", s.serveMCPHTTP)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"ok": "true", "boot": bootID})
	})
	return mux
}

func (s *mcpServer) serveMCPHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		// GET (SSE ストリーム) と DELETE (セッション終了) は使わない。
		// 405 は仕様上許されていて、クライアントは単発 POST に落ちる
		w.Header().Set("Allow", "POST")
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, maxHTTPRequestBytes))
	if err != nil {
		writeRPCError(w, http.StatusBadRequest, -32700, "本文を読めない: "+err.Error())
		return
	}
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		writeRPCError(w, http.StatusBadRequest, -32700, "JSON として解釈できない: "+err.Error())
		return
	}

	// method だけ残す。opencode 側は接続状態を connected のまま持ち続けるので、
	// 「本当に届いているか」はこのログでしか分からない。本文は出さない
	log.Printf("<- %s", req.Method)

	out := s.handleBuffered(r.Context(), req)
	if len(out) == 0 {
		// 通知には応答本文が無い。202 が streamable HTTP の作法
		w.WriteHeader(http.StatusAccepted)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Mcp-Session-Id", bootID)
	_, _ = w.Write(append(out, '\n'))
}

func writeRPCError(w http.ResponseWriter, status, code int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(rpcResponse{
		JSONRPC: "2.0", Error: &rpcError{Code: code, Message: message},
	})
}

// serveHTTPMCP は listen で待ち受ける。戻らない。
func serveHTTPMCP(s *mcpServer, listen string) error {
	srv := &http.Server{
		Addr:              listen,
		Handler:           s.httpMux(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	log.Printf("MCP を HTTP で待ち受ける: http://%s/mcp (boot=%s)", listen, bootID)
	if err := srv.ListenAndServe(); err != nil {
		return fmt.Errorf("待ち受けに失敗: %w", err)
	}
	return nil
}

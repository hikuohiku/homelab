// MCP stdio サーバ。コアに「homelab を見る目」と「heart に仕事を頼む口」を与える。
//
//	homelab_status       — autopilot 自身の状態 (走行中エージェント / プロジェクト / 要対応 / 心拍 / 当日消費)
//	homelab_health       — ArgoCD Application / Pod / PVC / Node の健全性 (30 分ごとのレポート)
//	homelab_applications — ArgoCD Application の sync/health を live で (k8s API 直読み)
//	homelab_pods         — 全 namespace の Pod を live で
//	homelab_events       — 直近の Warning 系 Event を live で
//	request_task         — 実装依頼を heart のパイプラインに載せる (bus へ publish)
//	dispatch_task        — いま着手してほしい仕事を heart に同期で要求する (admission gate)
//
// 読み取りはすべて引数を取らない。汎用の HTTP fetch や kubectl を与えるのではなく
// 用途を固定した窓を開けるだけにしてあるのは、コアが到達できる先を設定ではなく
// コードで縛るため。新しい credential も RBAC も要らない:
//
//   - status はクラスタ内の ops-dashboard (認証不要・read-only の API)
//   - health はクラスタ内の ConfigMap ops-health-report を読む (設計 state-out-of-git
//     Phase 5)。ConfigMap に届かないときだけ ops-health-report ブランチに落ちる
//   - live の 3 つは k8s API を read-only の ClusterRole (autopilot-reader) で直読みする。
//     トークンは projected volume で**このサイドカーにだけ** mount してあり、
//     opencode コンテナからは見えない (k8s.go の冒頭を参照)
//
// request_task も同じ思想で、**依頼を出す以上のことはできない**。Job の種類も
// モデルも優先度も引数に無く、採否・実行・納品の判断はすべて heart の領分のまま
// (設計 D3/D7)。コアは git にも K8s にも触らない。
//
// 返すのは取得した JSON そのまま。要約はコアの仕事で、ここでは加工しない
// (加工すると「取れなかった」と「空だった」の区別が消える)。
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
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
		{
			Name: "homelab_applications",
			Description: "ArgoCD Application の sync/health を **いまこの瞬間** の値で返す。引数は取らない。" +
				"homelab_health は 30 分ごとのレポートなので、ズレを疑うときや直後の変化を見たいときはこちらを使う。",
			InputSchema: noArgsSchema(),
		},
		{
			Name: "homelab_pods",
			Description: "全 namespace の Pod を live で返す (phase / ready / 再起動回数 / ノード / 理由)。" +
				"引数は取らない。CrashLoopBackOff や Pending の実物を見るときに使う。",
			InputSchema: noArgsSchema(),
		},
		{
			Name: "homelab_events",
			Description: "Normal でない k8s Event を新しい順で返す。引数は取らない。" +
				"OOMKilled・スケジュール失敗・probe 失敗の「起きた瞬間」がここに出る。" +
				"Event は 1 時間程度で消えるので、空でも「異常が無かった」ではない。",
			InputSchema: noArgsSchema(),
		},
		{
			Name: "request_task",
			Description: "実装・変更の依頼を heart のタスク依頼キューに載せる。" +
				"あなたはこれで起票できるが、実装するのは heart 配下の runner であり、" +
				"採択されるとは限らない。「やっておきます」と約束しないこと。" +
				"同じ内容の依頼を繰り返しても 1 件として扱われる。" +
				"Job の種類・モデル・優先度は選べない (heart の判断領域)。",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"title": map[string]any{
						"type":        "string",
						"description": "何の依頼かを 1 行で。プロジェクト一覧に載る題名。",
					},
					"body": map[string]any{
						"type": "string",
						"description": "何をどうしたいか、なぜ要るか。" +
							"立案役が読む原料なので、所有者の言葉と観測した事実を残すこと。",
					},
				},
				"required": []string{"title", "body"},
			},
		},
		{
			Name: "dispatch_task",
			Description: "いま着手してほしい仕事を heart に**同期で**要求する。数秒で可否が返る。" +
				"受理されれば heart が実装役をそのまま起動する。" +
				"断られたら理由が返るので、そのまま所有者に伝えること。" +
				"起動するのは heart であり、あなたが実装するのではない。" +
				"同じ内容を何度要求しても 1 件として扱われる。" +
				"実行役の種類・思考エンジン・優先度・権限は選べない (heart の判断領域)。" +
				"急がない依頼は request_task の方が適切。",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"title": map[string]any{
						"type":        "string",
						"description": "何をするかを 1 行で。プロジェクト一覧に載る題名。",
					},
					"body": map[string]any{
						"type": "string",
						"description": "何をどうしたいか、なぜ要るか、どこを見れば分かるか。" +
							"実装役が読む唯一の仕様なので、所有者の言葉と観測した事実を残すこと。",
					},
				},
				"required": []string{"title", "body"},
			},
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

// fetchHealth は健全性レポートを取る。正はクラスタ内の ConfigMap で、そこへ届かない
// ときだけ GitHub のブランチに落ちる (ブランチ経路は設計 Phase 7 で消える)。
// **どちらも取れなければエラーにする** — 取れなかったことを「異常なし」に化けさせない。
func (s *mcpServer) fetchHealth(ctx context.Context) (string, error) {
	k, err := s.kubeAPI()
	if err == nil {
		raw, cmErr := k.healthReport(ctx,
			envOr("CORE_HEALTH_NAMESPACE", "ops-health-reporter"),
			envOr("CORE_HEALTH_CONFIGMAP", "ops-health-report"),
			envOr("CORE_HEALTH_KEY", "latest.json"))
		if cmErr == nil {
			return clip(raw), nil
		}
		err = cmErr
	}
	log.Printf("health: ConfigMap から読めないのでブランチに落ちる: %v", err)
	raw, branchErr := s.client.fetchHealth(ctx)
	if branchErr != nil {
		return "", fmt.Errorf("health レポートを ConfigMap からもブランチからも読めない (%v / %w)", err, branchErr)
	}
	return raw, nil
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
	// out は応答の書き出し先。stdio では stdout、HTTP では 1 リクエストごとの
	// バッファへ差し替える (mcp_http.go の handleBuffered)。mu はその差し替えの排他
	out *json.Encoder
	mu  sync.Mutex
	// dispatch は command を heart へ渡す経路。既定は NATS への JetStream publish で、
	// テストでは差し替える。nil のときだけ遅延で接続する (バス未設定でも
	// 読み取りツールは使えるようにするため)
	dispatch func(commandEvent) error
	pub      *busPublisher
	now      func() time.Time
	// kube は k8s API への read 専用の口。最初に使うときだけ作る
	// (トークンが無い構成でも他のツールは動くようにするため)。
	kube    *kubeClient
	kubeErr error
	kubeMu  sync.Mutex
}

// kubeAPI は k8s クライアントを遅延で用意する。作れなかった理由は覚えておいて
// 毎回同じことを返す (取れないことを isError で伝えるのはツール側の仕事)。
func (s *mcpServer) kubeAPI() (*kubeClient, error) {
	s.kubeMu.Lock()
	defer s.kubeMu.Unlock()
	if s.kube != nil || s.kubeErr != nil {
		return s.kube, s.kubeErr
	}
	s.kube, s.kubeErr = newKubeClient()
	return s.kube, s.kubeErr
}

// publishCommand は command を 1 件流す。接続は最初の依頼まで張らない。
func (s *mcpServer) publishCommand(e commandEvent) error {
	if s.dispatch != nil {
		return s.dispatch(e)
	}
	if s.pub == nil {
		p, err := connectPublisher()
		if err != nil {
			return fmt.Errorf("バスに繋げない: %w", err)
		}
		if p == nil {
			return errors.New("NATS_URL / NATS_NKEY_SEED が未設定で、heart への経路が無い")
		}
		s.pub = p
	}
	return s.pub.publish(e)
}

// requestTask は task-request を 1 件 heart へ渡す。
// 検証に落ちた依頼も、流せなかった依頼も、成功と取り違えないよう error で返す。
func (s *mcpServer) requestTask(args json.RawMessage) (string, error) {
	var p struct {
		Title string `json:"title"`
		Body  string `json:"body"`
	}
	if len(args) > 0 {
		if err := json.Unmarshal(args, &p); err != nil {
			return "", fmt.Errorf("引数を解釈できない: %w", err)
		}
	}
	now := time.Now
	if s.now != nil {
		now = s.now
	}
	ev, err := newTaskRequest(p.Title, p.Body, now())
	if err != nil {
		return "", err
	}
	if err := s.publishCommand(ev); err != nil {
		return "", err
	}
	// 「起票した」だけを言う。着手も採択も約束しない
	return fmt.Sprintf(
		"タスク依頼を heart のキューに載せた (command_id=%s, title=%s)。"+
			"採択するかどうかは heart が判断する。着手を約束しないこと。",
		ev.CommandID, ev.Title), nil
}

func (s *mcpServer) respond(id json.RawMessage, result any) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Result: result})
}

func (s *mcpServer) respondError(id json.RawMessage, code int, message string) {
	_ = s.out.Encode(rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: code, Message: message}})
}

// callKube は live の k8s 読み取り 3 種を捌く。
func (s *mcpServer) callKube(ctx context.Context, name string) (string, error) {
	k, err := s.kubeAPI()
	if err != nil {
		return "", err
	}
	switch name {
	case "homelab_applications":
		return k.applications(ctx)
	case "homelab_pods":
		return k.pods(ctx)
	default:
		return k.events(ctx)
	}
}

func (s *mcpServer) callTool(ctx context.Context, name string, args json.RawMessage) toolResult {
	var (
		body string
		err  error
	)
	switch name {
	case "homelab_status":
		body, err = s.client.fetchStatus(ctx)
	case "homelab_health":
		body, err = s.fetchHealth(ctx)
	case "homelab_applications", "homelab_pods", "homelab_events":
		body, err = s.callKube(ctx, name)
	case "request_task":
		body, err = s.requestTask(args)
		if err != nil {
			// 送れなかったことを isError で返す。ここを握り潰すと、コアが
			// 起票できていないのに「依頼しておきました」と人間に言う
			return toolResult{
				Content: []textContent{{Type: "text", Text: "依頼を出せなかった: " + err.Error()}},
				IsError: true,
			}
		}
		return toolResult{Content: []textContent{{Type: "text", Text: body}}}
	case "dispatch_task":
		// 拒否も到達不能も isError で返す。ここを握り潰すと、コアが着手して
		// いないのに「着手しました」と人間に言う
		text, ok := s.dispatchTask(ctx, args)
		return toolResult{Content: []textContent{{Type: "text", Text: text}}, IsError: !ok}
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
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			s.respondError(req.ID, -32602, "params を解釈できない: "+err.Error())
			return
		}
		s.respond(req.ID, s.callTool(ctx, p.Name, p.Arguments))

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

// runMCP は MCP サーバを 1 つ起動する。listen が空なら stdio、
// そうでなければ HTTP streamable でその addr を待ち受ける。
func runMCP(listen string) {
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
	defer func() { server.pub.close() }()
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

// telegram-adapter — Telegram DM を NATS のイベント経路へ流す決定論アダプタ。
//
// OpenClaw (P-0090 / P-0107) の置き換え。あちらは受信のために agent runtime
// (9 プラグイン・63 コマンド・自前 LLM) を丸ごと抱えていたが、実際に必要なのは
// 「allowlist の送信者からの private DM を、判断せずそのまま保存する」だけだった。
//
// このプロセスに判定ロジックは存在しない。停止・veto・依頼の判定は heart の triage
// (将来は常駐コア) が行う。ここがやるのは以下だけ:
//
//	getUpdates long poll → allowlist + private 判定 → note へ変換 → NATS へ publish
//
// 以前は同じ note を GitHub (ops-feedback ブランチの inbox) にも PUT していた。状態が
// git から出るのに合わせて閉じた (設計 state-out-of-git Phase 7)。**このプロセスは
// もう git を触らない。**
//
// 設計上の要点:
//
//   - **冪等性は note ID で担保する**。ID に update_id を埋めるため、同じ update を
//     二度処理しても同じ Nats-Msg-Id になり、JetStream 側が重複を落とす。
//     これにより cursor の永続化 (PVC) が不要になった。
//
//   - **未処理の update は起動時にすべて処理する**。Telegram は offset で ack された
//     update だけをキューから消すため、ダウン中に届いた分は復帰後に受け取れる。
//     「初回は履歴を捨てる」ロジックは持たない — 冪等なので取りこぼしを避ける側に倒す。
//
//   - **private チャット限定**。OpenClaw の dmPolicy: allowlist が担っていた
//     グループ遮断をこちらで持つ。送信者 ID の一致だけでは、許可ユーザーが bot を
//     グループに招いた場合にそのグループの発言を拾ってしまう。
//
//   - **fail-closed**。TELEGRAM_ALLOWED_USER_ID が未設定・非数値なら 1 件も取り込まない。
//     NATS が未設定でも起動しない — 唯一の出口が無い状態で待機すると、受信した
//     書き置きを黙って捨てながら ack だけ返し続けることになる。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// --- 設定 (環境変数。既定値はこの Pod の実配置) ---

type config struct {
	botToken     string
	allowedUser  int64
	hasAllowUser bool
	ackText      string
	pollSeconds  int
	telegramAPI  string
}

// 同じ update で連続失敗したら諦めて先へ進める閾値。Telegram 側のキューに原本は
// 24h 残るので、壊れた 1 件で後続を永久に止めるより流れを守る。
const maxAttempts = 3

// loadConfig は環境変数から設定を読む。adapter モードと mcp モードで同じ設定を使う
// (受信と送信で allowlist の解釈がずれると事故になる)。
func loadConfig() (*config, error) {
	c := &config{
		ackText:     os.Getenv("TELEGRAM_ACK_TEXT"),
		telegramAPI: strings.TrimSuffix(envOr("TELEGRAM_API", "https://api.telegram.org"), "/"),
	}
	c.pollSeconds = 50
	if raw := os.Getenv("ADAPTER_POLL_SECONDS"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 && n <= 300 {
			c.pollSeconds = n
		}
	}

	c.botToken = strings.TrimSpace(os.Getenv("TELEGRAM_BOT_TOKEN"))
	if c.botToken == "" {
		return nil, errors.New("TELEGRAM_BOT_TOKEN が空です")
	}
	// fail-closed: 未設定・非数値なら誰にも一致させない (保存 0 件のまま待機する)
	if raw := strings.TrimSpace(os.Getenv("TELEGRAM_ALLOWED_USER_ID")); raw != "" {
		if id, err := strconv.ParseInt(raw, 10, 64); err == nil {
			c.allowedUser = id
			c.hasAllowUser = true
		}
	}
	return c, nil
}

func envOr(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

// --- Telegram の受信データ ---

type Update struct {
	UpdateID int64    `json:"update_id"`
	Message  *Message `json:"message"`
}

type Message struct {
	From    *User  `json:"from"`
	Chat    *Chat  `json:"chat"`
	Date    int64  `json:"date"`
	Text    string `json:"text"`
	Caption string `json:"caption"`
}

type User struct {
	ID int64 `json:"id"`
}

type Chat struct {
	ID   int64  `json:"id"`
	Type string `json:"type"`
}

// --- 純関数 (テストが固定する契約) ---

// extractText は生テキストを加工せずに返す。text 無しメディアは caption を見る。
// どちらも無ければ空 (スタンプ・位置情報など「生テキストが存在しない」ものは対象外)。
func extractText(u Update) string {
	if u.Message == nil {
		return ""
	}
	for _, v := range []string{u.Message.Text, u.Message.Caption} {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// isAllowed は「allowlist の送信者」かつ「private チャット」のときだけ true。
// hasAllow が false (env 未設定・非数値) なら常に false = fail-closed。
func isAllowed(u Update, allowedUser int64, hasAllow bool) bool {
	if !hasAllow || u.Message == nil {
		return false
	}
	if u.Message.Chat == nil || u.Message.Chat.Type != "private" {
		return false
	}
	if u.Message.From == nil {
		return false
	}
	return u.Message.From.ID == allowedUser
}

// Note は書き置き 1 件。ops-dashboard の /api/feedback が publish する形と同じで、
// 読み手 (heart のサイドカー / コア) は出所で形を変えなくてよい。
type Note struct {
	ID       string `json:"id"`
	Source   string `json:"source"`
	Received string `json:"received"`
	Body     string `json:"body"`
}

// noteID は update_id を埋めた決定論 ID を返す。ダッシュボード側の
// "YYYYMMDD-HHMMSS-<hex>" と同じ形を保ちつつ、乱数ではなく update_id 由来にすることで
// 再処理時に同じ ID になる (= 下流が重複を落とせる)。
func noteID(received time.Time, updateID int64) string {
	return fmt.Sprintf("%s-%06x", received.UTC().Format("20060102-150405"), updateID)
}

func buildNote(received time.Time, updateID int64, body string) Note {
	return Note{
		ID:       noteID(received, updateID),
		Source:   "telegram",
		Received: received.UTC().Format("2006-01-02T15:04:05Z"),
		Body:     body,
	}
}

// messageTime は Telegram の date (epoch 秒) を返す。欠けていれば now に落とす。
func messageTime(u Update, now time.Time) time.Time {
	if u.Message != nil && u.Message.Date > 0 {
		return time.Unix(u.Message.Date, 0).UTC()
	}
	return now.UTC()
}

// --- IO ---

type client struct {
	cfg  *config
	http *http.Client
}

func newClient(cfg *config) *client {
	return &client{
		cfg: cfg,
		// long poll (既定 50s) より十分長く取る
		http: &http.Client{Timeout: time.Duration(cfg.pollSeconds+20) * time.Second},
	}
}

func (c *client) telegram(ctx context.Context, method string, params url.Values) ([]byte, error) {
	endpoint := fmt.Sprintf("%s/bot%s/%s", c.cfg.telegramAPI, c.cfg.botToken, method)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint,
		strings.NewReader(params.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("telegram %s: status %d: %s", method, resp.StatusCode, truncate(string(body), 200))
	}
	return body, nil
}

// getUpdates は offset 以降の update を long poll で取る。offset を渡した時点で
// Telegram 側は offset 未満を ack 済みとして破棄する (= サーバー側 cursor)。
func (c *client) getUpdates(ctx context.Context, offset int64) ([]Update, error) {
	params := url.Values{}
	if offset > 0 {
		params.Set("offset", strconv.FormatInt(offset, 10))
	}
	params.Set("timeout", strconv.Itoa(c.cfg.pollSeconds))
	params.Set("allowed_updates", `["message"]`)

	raw, err := c.telegram(ctx, "getUpdates", params)
	if err != nil {
		return nil, err
	}
	var parsed struct {
		OK          bool     `json:"ok"`
		Result      []Update `json:"result"`
		Description string   `json:"description"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, err
	}
	if !parsed.OK {
		return nil, fmt.Errorf("telegram getUpdates: ok=false: %s", truncate(parsed.Description, 200))
	}
	return parsed.Result, nil
}

func (c *client) sendAck(ctx context.Context, chatID int64) {
	if c.cfg.ackText == "" {
		return
	}
	params := url.Values{}
	params.Set("chat_id", strconv.FormatInt(chatID, 10))
	params.Set("text", c.cfg.ackText)
	if _, err := c.telegram(ctx, "sendMessage", params); err != nil {
		// ack は補助。失敗しても保存経路は続ける
		log.Printf("ack 送信に失敗 (継続): %v", err)
	}
}

// --- メインループ ---

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// main は 2 つのモードを持つ。
//
//	(引数なし) — adapter: 受信を NATS のイベント経路へ流す常駐ループ
//	mcp        — 返信ツール: MCP stdio サーバとして telegram_reply を提供する
//
// 同じバイナリに同居させるのは、Telegram の呼び出し方と allowlist の解釈を
// 一箇所に保つため (受信と送信で判定がずれると事故になる)。
func main() {
	if len(os.Args) > 1 && os.Args[1] == "mcp" {
		listen, err := parseMCPListen(os.Args[2:])
		if err != nil {
			log.Fatalf("起動できません: %v", err)
		}
		runMCP(listen)
		return
	}
	runAdapter()
}

func runAdapter() {
	log.SetFlags(0)
	log.SetPrefix("[telegram-adapter] ")

	cfg, err := loadConfig()
	if err != nil {
		log.Fatalf("起動できません: %v", err)
	}
	if !cfg.hasAllowUser {
		log.Print("TELEGRAM_ALLOWED_USER_ID 未設定/非数値: fail-closed で何も取り込まない待機状態")
	}
	log.Printf("開始 (poll=%ds ack=%t)", cfg.pollSeconds, cfg.ackText != "")

	c := newClient(cfg)
	ctx := context.Background()

	// 唯一の出口 (設計 state-out-of-git Phase 7 で GitHub 側を閉じた)。
	// 未設定は起動時に落とす — 出口が無いまま待機すると、受信した書き置きを
	// 黙って捨てながら ack だけ返し続けることになる
	bus, err := connectBus()
	if err != nil {
		log.Fatalf("NATS に繋げません: %v", err)
	}
	defer bus.close()
	log.Printf("NATS に接続 (subject=%s)", bus.subject)

	var offset int64
	attempts := map[int64]int{}

	for {
		updates, err := c.getUpdates(ctx, offset)
		if err != nil {
			log.Printf("getUpdates 失敗 (5s 後に再試行): %v", err)
			time.Sleep(5 * time.Second)
			continue
		}

		for _, u := range updates {
			// 対象外 (他人・グループ・テキスト無し) はここで ack して捨てる。
			// 判定は決定論のみ — キーワードも LLM も使わない
			body := extractText(u)
			if body == "" || !isAllowed(u, cfg.allowedUser, cfg.hasAllowUser) {
				offset = u.UpdateID + 1
				continue
			}

			note := buildNote(messageTime(u, time.Now()), u.UpdateID, body)
			// publish が通るまで offset を進めない。ここが唯一の出口になったので、
			// 「送れなかったが受信済み」にすると書き置きが消える。
			// JetStream の ack を待つので、届いていないことはここに返る
			if err := bus.publish(busEvent{
				ID: note.ID, Source: note.Source, Received: note.Received,
				Body: note.Body, UpdateID: u.UpdateID,
			}); err != nil {
				attempts[u.UpdateID]++
				log.Printf("update %d: publish 失敗 %d/%d: %v", u.UpdateID, attempts[u.UpdateID], maxAttempts, err)
				if attempts[u.UpdateID] >= maxAttempts {
					log.Printf("update %d: %d 回失敗したため skip", u.UpdateID, maxAttempts)
					delete(attempts, u.UpdateID)
					offset = u.UpdateID + 1
					continue
				}
				// 順序を守るため、失敗した直後は offset を進めない
				break
			}
			log.Printf("published %s (%s, update %d, %d chars)", bus.subject, note.ID, u.UpdateID, len(body))

			delete(attempts, u.UpdateID)
			offset = u.UpdateID + 1
			if u.Message.Chat != nil {
				c.sendAck(ctx, u.Message.Chat.ID)
			}
		}
	}
}

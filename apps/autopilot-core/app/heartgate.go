// heart の admission gate を叩く口 (設計 rev3 Phase D)。
//
// コアが「いま着手してほしい」を **同期で** 頼み、数秒で可否の答えを得るための
// 経路。判定も Job 作成も heart のままで、コアに k8s の write 権限は渡らない
// (設計 D29)。ここがやるのは HTTP を 1 往復することだけ。
//
// なぜ NATS の request-reply でないか:
//
//	heart 自身は NATS を話さない。同居する Go のサイドカー
//	(apps/autopilot/bus-sidecar) がイベントをファイルに落とし、heart が
//	ファイルとして読む片方向の経路になっている。同期の返答を返すには
//	サイドカーに返信の口・新しい subject の ACL・heart 側の待ち合わせを
//	足すことになり、**HTTP を 1 本開けるより増える面が多い**。
//
// 到達範囲と認証:
//
//	ClusterIP Service (autopilot-heart.autopilot.svc:8099) だけ。Ingress も
//	Tailscale も通さない。トークンは持たない — 持たせると ops/rules.json の
//	allowed_autopilot_doppler_keys に鍵を足すことになり、そこは人間レビュー
//	必須のパスだから。送信元は NetworkPolicy でこの Pod に限る
//	(apps/autopilot/heart-service.yaml)。
//
// heart が落ちているとき:
//
//	isError で返し、既存の request_task (バス経由の起票) に案内する。
//	request_task は heart が死んでいても NATS に積めるので、依頼が消えない。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// 同期呼び出しの上限。判定だけなのでミリ秒で返るはず。
// 数秒待って返らなければ heart の異常
const heartGateTimeout = 10 * time.Second

type gateRequest struct {
	Title string `json:"title"`
	Body  string `json:"body"`
}

type gateResponse struct {
	Status     string `json:"status"`
	Reason     string `json:"reason"`
	Message    string `json:"message"`
	DispatchID string `json:"dispatch_id"`
	ProjectID  string `json:"project_id"`
}

func heartGateURL() string {
	base := strings.TrimSuffix(
		envOr("CORE_HEART_GATE_URL", "http://autopilot-heart.autopilot.svc:8099"), "/")
	return base + "/dispatch"
}

// newGateRequest は引数を検証して要求を組む。検証に落ちたら送らない
// (heart 側でも同じ検査をするが、往復せずに理由を返せる方が速い)。
// 受入検証 (verify) は取らない。2026-08-24 の所有者判断で dispatch 経路から
// 外した — verify を書くのも LLM なので、いくらでも迂回できる検査だった。
func newGateRequest(title, body string) (gateRequest, error) {
	req := gateRequest{Title: strings.TrimSpace(title), Body: strings.TrimSpace(body)}
	if req.Title == "" {
		return req, fmt.Errorf("title が空。何をするのか 1 行で書くこと")
	}
	if req.Body == "" {
		return req, fmt.Errorf("body が空。何をどうしたいかを書くこと")
	}
	if n := len([]rune(req.Title)); n > maxCommandTitleRunes {
		return req, fmt.Errorf("title が長すぎる (%d 文字 > %d)", n, maxCommandTitleRunes)
	}
	if n := len([]rune(req.Body)); n > maxCommandBodyRunes {
		return req, fmt.Errorf("body が長すぎる (%d 文字 > %d)。要点に絞ること", n, maxCommandBodyRunes)
	}
	return req, nil
}

// callHeartGate は 1 往復する。到達できない・解釈できない応答は error。
// **拒否は error ではない** — heart が理由を添えて答えた正常な結果なので、
// 呼び出し側が人語のまま返す。
func (s *mcpServer) callHeartGate(ctx context.Context, req gateRequest) (gateResponse, error) {
	raw, err := json.Marshal(req)
	if err != nil {
		return gateResponse{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, heartGateTimeout)
	defer cancel()
	httpReq, err := http.NewRequestWithContext(
		ctx, http.MethodPost, heartGateURL(), bytes.NewReader(raw))
	if err != nil {
		return gateResponse{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := s.client.http.Do(httpReq)
	if err != nil {
		return gateResponse{}, fmt.Errorf("heart の admission gate に届かない: %w", err)
	}
	defer resp.Body.Close()
	var out gateResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return gateResponse{}, fmt.Errorf("heart の応答を解釈できない (status=%d): %w", resp.StatusCode, err)
	}
	if out.Status == "" {
		return out, fmt.Errorf("heart が status を返さなかった (status=%d): %s", resp.StatusCode, out.Message)
	}
	return out, nil
}

// dispatchTask は dispatch_task ツールの本体。
// 返り値の bool は「コアに成功として見せてよいか」。false なら isError で返す。
func (s *mcpServer) dispatchTask(ctx context.Context, args json.RawMessage) (string, bool) {
	var p struct {
		Title string `json:"title"`
		Body  string `json:"body"`
	}
	if len(args) > 0 {
		if err := json.Unmarshal(args, &p); err != nil {
			return "引数を解釈できない: " + err.Error(), false
		}
	}
	req, err := newGateRequest(p.Title, p.Body)
	if err != nil {
		return "着手を頼めなかった: " + err.Error(), false
	}
	res, err := s.callHeartGate(ctx, req)
	if err != nil {
		// heart が居なくても依頼を捨てない。冷スペアの経路を必ず案内する
		return "heart に着手を頼めなかった: " + err.Error() +
			"。heart が落ちている可能性がある。急がないなら request_task で" +
			"キューに起票できる (バス経由なので heart が死んでいても積める)。" +
			"**着手したとは言わないこと。**", false
	}
	switch res.Status {
	case "accepted":
		return fmt.Sprintf(
			"heart が着手を受理した (%s, dispatch_id=%s)。%s "+
				"runner Job がそのまま走る。結果は "+
				"homelab_status に出るので、聞かれたらそこを見ること。"+
				"同じ内容でこのツールを呼び直せば現在の扱いを聞ける (二重には着手しない)。",
			res.ProjectID, res.DispatchID, res.Message), true
	case "duplicate":
		return fmt.Sprintf("同じ依頼は既に受理済み: %s", res.Message), true
	default:
		// 拒否。理由を人語のまま渡し、**成功と取り違えさせない**
		return fmt.Sprintf(
			"heart が着手を断った (%s): %s。**着手したとは言わないこと。**",
			res.Reason, res.Message), false
	}
}

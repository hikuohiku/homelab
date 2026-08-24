// homelab_proposals の契約 (設計 state-out-of-git 4b-1)。
//
// 守りたいのは 3 点:
//   - **要点だけ**を返す。why / dod / verify が混ざると 390 件で文脈が飛ぶ
//   - 件数の上限が効く。引数で無制限にできない
//   - 棄却案の reject_reason / improve_hint が生きて届く (教師信号の唯一の経路)
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// projectItems は CR の items を組み立てる。spec は projects.json の 1 エントリ
// そのままで、立案時の spec がその中の spec に入る (crd-project.yaml と同じ形)。
func projectItems(t *testing.T, items ...map[string]any) string {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"items": items})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func project(id, state string, inner map[string]any) map[string]any {
	return map[string]any{
		"metadata": map[string]any{"name": strings.ToLower(id)},
		"spec": map[string]any{
			"id":    id,
			"title": id + " のタイトル",
			"state": state,
			"why":   "この長い why は返ってきてはいけない",
			"spec":  inner,
		},
	}
}

func rejected(id, reason, hint string, cell ...string) map[string]any {
	return project(id, "rejected", map[string]any{
		"adopted":       false,
		"cell":          cell,
		"reject_reason": reason,
		"improve_hint":  hint,
		"proposed_at":   "2026-08-20T00:00:00Z",
		"why":           "立案時の why も返さない",
		"dod":           "dod も返さない",
		"verify":        []string{"true"},
		"_note_verify":  "無視されるべき",
	})
}

type proposalsResponse struct {
	Total     int    `json:"total"`
	Matched   int    `json:"matched"`
	Returned  int    `json:"returned"`
	Truncated bool   `json:"truncated"`
	Note      string `json:"note"`
	Proposals []struct {
		ID           string   `json:"id"`
		Title        string   `json:"title"`
		Cell         []string `json:"cell"`
		Adopted      bool     `json:"adopted"`
		State        string   `json:"state"`
		RejectReason string   `json:"reject_reason"`
		ImproveHint  string   `json:"improve_hint"`
	} `json:"proposals"`
}

// callProposals は引数付きで homelab_proposals を呼ぶ。
func callProposals(t *testing.T, body, args string) proposalsResponse {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Path, "/autopilot.homelab.hikuohiku.dev/v1/namespaces/autopilot/projects") {
			t.Errorf("Project CR を見るべき: %q", r.URL.Path)
		}
		if r.Method != http.MethodGet {
			t.Errorf("read のみ (D29): %s", r.Method)
		}
		_, _ = w.Write([]byte(body))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	in := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"homelab_proposals","arguments":` + args + `}}`
	if err := s.serve(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	res := resultOf(t, firstResponse(t, out.String()))
	if res.IsError {
		t.Fatalf("成功のはず: %+v", res)
	}
	var parsed proposalsResponse
	if err := json.Unmarshal([]byte(res.Content[0].Text), &parsed); err != nil {
		t.Fatalf("%v: %s", err, res.Content[0].Text)
	}
	return parsed
}

func TestProposalsReturnsOnlyTheGist(t *testing.T) {
	body := projectItems(t,
		rejected("P-0190", "verify が骨抜き", "実測できる形にする", "k8s", "repair"),
		project("P-0200", "delivered", map[string]any{
			"adopted": true, "cell": []string{"self", "feature"},
			"proposed_at": "2026-08-21T00:00:00Z",
		}),
	)
	got := callProposals(t, body, `{}`)

	if got.Total != 2 || got.Returned != 2 {
		t.Fatalf("件数: %+v", got)
	}
	// 新しい順 (id の降順)
	if got.Proposals[0].ID != "P-0200" {
		t.Fatalf("新しい順ではない: %+v", got.Proposals)
	}
	rej := got.Proposals[1]
	if rej.Adopted || rej.State != "rejected" {
		t.Fatalf("棄却案の採否がおかしい: %+v", rej)
	}
	if rej.RejectReason != "verify が骨抜き" || rej.ImproveHint != "実測できる形にする" {
		t.Fatalf("教師信号が届いていない: %+v", rej)
	}
	if !got.Proposals[0].Adopted {
		t.Fatalf("delivered は採択済みのはず: %+v", got.Proposals[0])
	}
	// 全文は返さない。ここが緩むと 390 件で文脈が飛ぶ
	for _, forbidden := range []string{"この長い why", "立案時の why", "dod も返さない"} {
		if strings.Contains(mustJSON(t, got), forbidden) {
			t.Fatalf("全文が漏れている (%s)", forbidden)
		}
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestProposalsLimitIsEnforced(t *testing.T) {
	items := make([]map[string]any, 0, proposalsMaxLimit+20)
	for i := 0; i < proposalsMaxLimit+20; i++ {
		items = append(items, rejected(idOf(i), "理由", "示唆", "self", "repair"))
	}
	body := projectItems(t, items...)

	// 既定
	if got := callProposals(t, body, `{}`); got.Returned != proposalsDefaultLimit || !got.Truncated {
		t.Fatalf("既定の上限が効いていない: %+v", got.Returned)
	}
	// 明示
	if got := callProposals(t, body, `{"limit":5}`); got.Returned != 5 {
		t.Fatalf("limit=5 が効いていない: %d", got.Returned)
	}
	// 上限超えは握り潰す。予算に当たればさらに減るが、増えることはない
	if got := callProposals(t, body, `{"limit":99999}`); got.Returned > proposalsMaxLimit || !got.Truncated {
		t.Fatalf("最大件数を超えて返している: %d", got.Returned)
	}
}

func TestProposalsNeverExceedTheResponseBudget(t *testing.T) {
	// 題名も死因も長い実物に近い形。件数上限だけだと clip が JSON の途中で
	// 切って、応答が丸ごと読めなくなる
	long := strings.Repeat("あ", 300)
	items := make([]map[string]any, 0, proposalsMaxLimit)
	for i := 0; i < proposalsMaxLimit; i++ {
		it := rejected(idOf(i), long, long, "self", "repair")
		it["spec"].(map[string]any)["title"] = long
		items = append(items, it)
	}
	got := callProposals(t, projectItems(t, items...), `{"limit":100}`)
	if got.Returned == 0 || got.Returned >= proposalsMaxLimit {
		t.Fatalf("予算で削れていない: %d", got.Returned)
	}
	if !got.Truncated {
		t.Fatalf("削ったのに truncated が立っていない: %+v", got)
	}
}

func TestFitProposalsAlwaysReturnsAtLeastOne(t *testing.T) {
	rows := []proposalRow{{ID: "P-0001", Title: strings.Repeat("x", 5000)}}
	got, over := fitProposals(rows, 10)
	if len(got) != 1 || over {
		t.Fatalf("1 件しか無いときは落とさない: %d %v", len(got), over)
	}
}

func idOf(i int) string {
	const digits = "0123456789"
	return "P-" + string([]byte{
		digits[(i/1000)%10], digits[(i/100)%10], digits[(i/10)%10], digits[i%10],
	})
}

func TestProposalsFiltersByCellAndVerdict(t *testing.T) {
	body := projectItems(t,
		rejected("P-0001", "理由 A", "示唆 A", "k8s", "repair"),
		rejected("P-0002", "理由 B", "示唆 B", "storage", "feature"),
		project("P-0003", "delivered", map[string]any{
			"adopted": true, "cell": []string{"k8s", "feature"},
		}),
	)

	got := callProposals(t, body, `{"cell":"k8s"}`)
	if got.Matched != 2 || got.Total != 3 {
		t.Fatalf("cell の絞り込みが効いていない: %+v", got)
	}

	got = callProposals(t, body, `{"rejected_only":true}`)
	if got.Matched != 2 {
		t.Fatalf("棄却案だけに絞れていない: %+v", got)
	}
	for _, p := range got.Proposals {
		if p.Adopted {
			t.Fatalf("採択案が混ざっている: %+v", p)
		}
	}

	got = callProposals(t, body, `{"cell":"k8s","rejected_only":true}`)
	if got.Matched != 1 || got.Proposals[0].ID != "P-0001" {
		t.Fatalf("両方の絞り込みが重ならない: %+v", got)
	}
}

func TestProposalsBadArgumentsFallBackToDefaults(t *testing.T) {
	// 引数が壊れていても「読めなかった」にしない。立案の途中で読めないと、
	// 生成役は過去案を知らないまま案を書き切ってしまう
	body := projectItems(t, rejected("P-0001", "理由", "示唆", "self", "repair"))
	got := callProposals(t, body, `"これはオブジェクトではない"`)
	if got.Returned != 1 {
		t.Fatalf("既定で答えるべき: %+v", got)
	}
}

func TestProposalsReportsUnreachableApi(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"message":"projects is forbidden"}`))
	}))
	defer server.Close()

	s, out := newMCP(t, &config{})
	s.kube = newKubeAgainst(server.URL)
	res := callToolWith(t, s, out, "homelab_proposals")
	if !res.IsError {
		// 取れなかったことを「過去案は無い」に化けさせない
		t.Fatalf("isError で返すべき: %+v", res)
	}
}

func TestClampProposalLimit(t *testing.T) {
	for _, c := range []struct{ in, want int }{
		{0, proposalsDefaultLimit},
		{-3, proposalsDefaultLimit},
		{7, 7},
		{proposalsMaxLimit + 1, proposalsMaxLimit},
	} {
		if got := clampProposalLimit(c.in); got != c.want {
			t.Fatalf("clampProposalLimit(%d) = %d, want %d", c.in, got, c.want)
		}
	}
}

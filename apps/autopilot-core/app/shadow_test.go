// 立案の shadow 実行の契約を固定する。
//
// ここで守りたいことは 2 つだけ:
//
//	(1) **副作用ゼロ**。git に書かない、PR を作らない、request_task を撃たない、
//	    Telegram に送らない。書くのは PVC の shadow ディレクトリだけ
//	(2) 起動条件が決定論であること (LLM の気分で走らない)
//
//	cd apps/autopilot-core/app && go test ./...
package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testShadowConfig() shadowConfig {
	return shadowConfig{
		enabled:       true,
		interval:      6 * time.Hour,
		timeout:       2 * time.Second,
		maxConcurrent: 6,
		namespace:     "autopilot",
		pollInterval:  10 * time.Millisecond,
	}
}

func TestShadowIsDisabledByDefault(t *testing.T) {
	// 既定は無効。有効化は env の明示だけで行う (CHARTER: 黙って動き出さない)
	for _, key := range []string{"CORE_SHADOW_CURRICULUM"} {
		t.Setenv(key, "")
	}
	if loadShadowConfig().enabled {
		t.Fatal("env 未設定なら shadow は無効であるべき")
	}
	t.Setenv("CORE_SHADOW_CURRICULUM", "0")
	if loadShadowConfig().enabled {
		t.Fatal("0 は無効であるべき")
	}
	t.Setenv("CORE_SHADOW_CURRICULUM", "1")
	if !loadShadowConfig().enabled {
		t.Fatal("1 で有効になるべき")
	}
}

func TestFreeSlotsMatchesHeartCounting(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	doc := projectsDoc{Projects: []shadowProject{
		{State: "delivered"},
		{State: "stalled"},
		{State: "vetoed"},
		{State: "active"},
		{State: "in_review"},
		// 拒否権窓で待っている announced はスロットを使っていない
		{State: "announced", VetoDeadline: "2026-08-25T12:00:00Z"},
		// 窓が切れた announced は数える
		{State: "announced", VetoDeadline: "2026-08-23T12:00:00Z"},
	}}
	if got := freeSlots(doc, 6, now); got != 3 {
		t.Fatalf("空きは 6 - 3 = 3 のはず: got %d", got)
	}
}

func TestShadowDueIsDeterministic(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	idle := projectsDoc{Projects: []shadowProject{{State: "active"}}}

	cfg := testShadowConfig()
	if due, _, _ := shadowDue(cfg, idle, time.Time{}, now); !due {
		t.Fatal("有効・空きあり・cursor 無しなら走るべき")
	}

	off := cfg
	off.enabled = false
	if due, _, why := shadowDue(off, idle, time.Time{}, now); due || !strings.Contains(why, "無効") {
		t.Fatalf("無効なら走らない: due=%v why=%q", due, why)
	}

	if due, _, why := shadowDue(cfg, idle, now.Add(-time.Hour), now); due || !strings.Contains(why, "間隔") {
		t.Fatalf("間隔が空いていなければ走らない: due=%v why=%q", due, why)
	}

	stopped := idle
	stopped.StopEngaged = true
	if due, _, why := shadowDue(cfg, stopped, time.Time{}, now); due || why != "stop_engaged" {
		t.Fatalf("「止めて」が効いている間は走らない: due=%v why=%q", due, why)
	}

	full := projectsDoc{Projects: []shadowProject{
		{State: "active"}, {State: "active"}, {State: "active"},
		{State: "active"}, {State: "active"}, {State: "active"},
	}}
	if due, _, why := shadowDue(cfg, full, time.Time{}, now); due || !strings.Contains(why, "空き") {
		t.Fatalf("パイプラインが埋まっていたら走らない: due=%v why=%q", due, why)
	}
}

func TestExtractTaskResultsFindsCompletedOnes(t *testing.T) {
	raw := []byte(`{"messages":[
	  {"parts":[{"text":"<task id=\"a\" state=\"running\"></task>"}]},
	  {"parts":[{"text":"<task id=\"a\" state=\"completed\"><task_result>{\"proposals\":[]}</task_result></task>"}]}
	]}`)
	got := extractTaskResults(raw)
	if len(got) != 1 || got[0] != `{"proposals":[]}` {
		t.Fatalf("完了した subtask の結果だけを拾うべき: %#v", got)
	}
	if extractTaskResults([]byte("not json")) != nil {
		t.Fatal("JSON でなければ何も返さない")
	}
}

func TestExtractJSONObjectSurvivesPreamble(t *testing.T) {
	doc, ok := extractJSONObject("以下が結果です。\n```json\n{\"proposals\":[{\"id\":\"P-1\"}]}\n```")
	if !ok {
		t.Fatal("前置き付きでも JSON を取り出せるべき")
	}
	if ids := idsOf(doc, "proposals"); len(ids) != 1 || ids[0] != "P-1" {
		t.Fatalf("id を拾えるべき: %#v", ids)
	}
	if _, ok := extractJSONObject("JSON はありません"); ok {
		t.Fatal("JSON が無ければ false")
	}
}

// --- shadow 実行そのもの ---

type shadowServers struct {
	opencode      *httptest.Server
	github        *httptest.Server
	kube          *httptest.Server
	heart         *httptest.Server
	mu            sync.Mutex
	opencodeCalls []string
	githubCalls   []string
	kubeCalls     []string
	bodies        []string
	sessionBodies []string
}

// newShadowServers は planner → judge が素直に完走する opencode と、
// Project CR を返す k8s API、doc 全体の状態を返す heart の /healthz を模す。
// GitHub も立てるが、shadow はもう状態を読みに行かない (副作用ゼロの確認用)。
// すべての要求を記録する。
func newShadowServers(t *testing.T, promptStatus int, projects string) *shadowServers {
	t.Helper()
	s := &shadowServers{}
	prompts := 0

	s.opencode = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := readAllString(r)
		s.mu.Lock()
		s.opencodeCalls = append(s.opencodeCalls, r.Method+" "+r.URL.Path)
		s.bodies = append(s.bodies, body)
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/session":
			s.sessionBodies = append(s.sessionBodies, body)
			s.mu.Unlock()
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"id":"ses_shadow"}`))
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/prompt_async"):
			prompts++
			s.mu.Unlock()
			w.WriteHeader(promptStatus)
		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/message"):
			n := prompts
			s.mu.Unlock()
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(shadowMessages(n)))
		default:
			s.mu.Unlock()
			t.Errorf("想定外の opencode 要求: %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(s.opencode.Close)

	s.github = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.githubCalls = append(s.githubCalls, r.Method+" "+r.URL.Path)
		s.mu.Unlock()
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(projects))
	}))
	t.Cleanup(s.github.Close)

	// 状態の読み先は Project CR (非終端だけ) と heart の /healthz に移った
	// (設計 state-out-of-git 4b-2a)。呼び出し側は従来どおり projects.json の形で
	// 渡し、ここで 2 つに割る — 同じ入力から同じ判断が出ることを見たいので
	var doc projectsDoc
	if err := json.Unmarshal([]byte(projects), &doc); err != nil {
		t.Fatalf("テストの projects が JSON でない: %v", err)
	}
	s.kube = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.kubeCalls = append(s.kubeCalls, r.Method+" "+r.URL.Path+"?"+r.URL.RawQuery)
		s.mu.Unlock()
		// API server の selector と同じ切り方をする。終端を返さないので、
		// 終端を数え落とすコードがあればここで露見する
		items := []string{}
		for _, p := range doc.Projects {
			if terminalStates[p.State] {
				continue
			}
			raw, _ := json.Marshal(map[string]any{
				"spec": map[string]any{"state": p.State, "veto_deadline": p.VetoDeadline},
			})
			items = append(items, string(raw))
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"items":[` + strings.Join(items, ",") + `]}`))
	}))
	t.Cleanup(s.kube.Close)

	s.heart = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := json.Marshal(map[string]any{
			"ok":                 true,
			"stop_engaged":       doc.StopEngaged,
			"last_curriculum_at": doc.LastCurriculumAt,
		})
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(raw)
	}))
	t.Cleanup(s.heart.Close)
	return s
}

func readAllString(r *http.Request) string {
	if r.Body == nil {
		return ""
	}
	buf := make([]byte, 1<<20)
	n, _ := r.Body.Read(buf)
	return string(buf[:n])
}

// shadowMessages は「prompt を n 回受けたら、完了した subtask が n 件見える」を模す。
func shadowMessages(n int) string {
	results := []string{
		`{\"proposals\":[{\"id\":\"P-9001\"},{\"id\":\"P-9002\"}]}`,
		`{\"scores\":[],\"adopted\":[{\"id\":\"P-9002\"}]}`,
	}
	parts := []string{}
	for i := 0; i < n && i < len(results); i++ {
		parts = append(parts,
			`{"text":"<task id=\"t`+string(rune('a'+i))+`\" state=\"completed\"><task_result>`+results[i]+`</task_result></task>"}`)
	}
	return `{"messages":[{"parts":[` + strings.Join(parts, ",") + `]}]}`
}

func newShadowClient(t *testing.T, s *shadowServers, dir string) *client {
	t.Helper()
	t.Setenv("CORE_HEART_GATE_URL", s.heart.URL)
	c := newClient(&config{
		opencodeURL: s.opencode.URL,
		stateDir:    dir,
		githubAPI:   s.github.URL,
		githubToken: "t",
		repo:        "hikuohiku/homelab",
		model:       "opencode-go/ox-alpha-free",
	})
	// 実クラスタを向かせない。トークン読み込みも差し替える
	// (テスト環境に projected volume は無い)
	c.kube = newKubeAgainst(s.kube.URL)
	return c
}

const idleProjects = `{"projects":[{"state":"active"}],"stop_engaged":false,"last_curriculum_at":"2026-08-24T09:00:00Z"}`

func TestShadowRunRecordsWithoutSideEffects(t *testing.T) {
	dir := t.TempDir()
	// prompt_async の成功は 204 No Content。ここを失敗と誤認すると 1 段目で止まる
	s := newShadowServers(t, http.StatusNoContent, idleProjects)
	c := newShadowClient(t, s, dir)

	if !c.maybeRunShadow(context.Background(), testShadowConfig(), time.Now()) {
		t.Fatal("条件を満たしているのに走らなかった")
	}

	// (1) 記録の中身: Job 版と突き合わせられる鍵を持つこと
	rec := readLastShadowRecord(t, filepath.Join(dir, "shadow", "curriculum.jsonl"))
	if rec.Error != "" {
		t.Fatalf("完走すべき: %s", rec.Error)
	}
	if rec.Schema != shadowSchema || rec.RunID == "" || rec.Date == "" {
		t.Fatalf("突き合わせ用の鍵が欠けている: %#v", rec)
	}
	if rec.JobLastCurriculumAt != "2026-08-24T09:00:00Z" {
		t.Fatalf("Job 版の最終立案時刻を記録すべき: %q", rec.JobLastCurriculumAt)
	}
	if strings.Join(rec.ProposalIDs, ",") != "P-9001,P-9002" {
		t.Fatalf("発散の id を記録すべき: %#v", rec.ProposalIDs)
	}
	if strings.Join(rec.AdoptedIDs, ",") != "P-9002" {
		t.Fatalf("採択の id を記録すべき: %#v", rec.AdoptedIDs)
	}

	// (2) GitHub へは GET しか出さない = PR も commit も作れない
	for _, call := range s.githubCalls {
		if !strings.HasPrefix(call, "GET ") {
			t.Fatalf("GitHub への書き込みが起きている: %s", call)
		}
	}

	// (3) 常駐セッションを触らない。使うのは自分で作った使い捨てだけ
	for _, call := range s.opencodeCalls {
		if strings.Contains(call, "ses_core") {
			t.Fatalf("常駐セッションに触れている: %s", call)
		}
	}

	// (4) 送信系のツールを一言も指示しない
	for _, body := range s.bodies {
		for _, forbidden := range []string{"telegram_reply", "request_task", "dispatch_task"} {
			if strings.Contains(body, forbidden) {
				t.Fatalf("shadow が %s に触れている: %s", forbidden, body)
			}
		}
	}

	// (5) 書き込み先は shadow ディレクトリだけ (git も作らない)
	written := []string{}
	_ = filepath.Walk(dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		rel, _ := filepath.Rel(dir, path)
		written = append(written, filepath.ToSlash(rel))
		return nil
	})
	for _, w := range written {
		if !strings.HasPrefix(w, "shadow/") {
			t.Fatalf("shadow 以外に書き込んでいる: %s (all=%v)", w, written)
		}
	}
}

func TestShadowAcceptsAll2xxFromPromptAsync(t *testing.T) {
	// 204 を失敗と誤認して同じ返事を 3 回送った事故 (2026-08-23) の回帰。
	// subtask 経路でも 2xx は一律で成功として扱う
	for _, code := range []int{200, 201, 202, 204} {
		dir := t.TempDir()
		s := newShadowServers(t, code, idleProjects)
		c := newShadowClient(t, s, dir)
		if err := c.promptSubtask(context.Background(), "ses_shadow", "planner", "d", "p"); err != nil {
			t.Fatalf("status=%d は成功として扱うべき: %v", code, err)
		}
	}
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusInternalServerError, idleProjects)
	c := newShadowClient(t, s, dir)
	if err := c.promptSubtask(context.Background(), "ses_shadow", "planner", "d", "p"); err == nil {
		t.Fatal("5xx は失敗として扱うべき")
	}
}

func TestShadowSessionCarriesNoPermission(t *testing.T) {
	// セッションレベルの permission は子へ伝播してサブエージェントの allow を潰す。
	// driver は POST /session に permission を渡してはいけない
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusNoContent, idleProjects)
	c := newShadowClient(t, s, dir)
	if _, err := c.newShadowSession(context.Background(), "shadow"); err != nil {
		t.Fatal(err)
	}
	if len(s.sessionBodies) != 1 {
		t.Fatalf("セッションを 1 つだけ作るべき: %v", s.sessionBodies)
	}
	if strings.Contains(s.sessionBodies[0], "permission") {
		t.Fatalf("POST /session に permission を渡してはいけない: %s", s.sessionBodies[0])
	}
}

func TestShadowUsesTheClosedShadowAgent(t *testing.T) {
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusNoContent, idleProjects)
	c := newShadowClient(t, s, dir)
	if err := c.promptSubtask(context.Background(), "ses_shadow", "planner", "発散", "本文"); err != nil {
		t.Fatal(err)
	}
	var payload struct {
		Agent string `json:"agent"`
		Parts []struct {
			Type  string `json:"type"`
			Agent string `json:"agent"`
		} `json:"parts"`
	}
	last := s.bodies[len(s.bodies)-1]
	if err := json.Unmarshal([]byte(last), &payload); err != nil {
		t.Fatalf("要求本文が JSON でない: %s", last)
	}
	if payload.Agent != shadowAgentName {
		t.Fatalf("受け皿は閉じた shadow エージェントであるべき: %q", payload.Agent)
	}
	if len(payload.Parts) != 1 || payload.Parts[0].Type != "subtask" || payload.Parts[0].Agent != "planner" {
		t.Fatalf("subtask パートで名指し起動すべき: %#v", payload.Parts)
	}
}

func TestShadowSkipsWhenStopEngaged(t *testing.T) {
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusNoContent,
		`{"projects":[],"stop_engaged":true}`)
	c := newShadowClient(t, s, dir)
	if c.maybeRunShadow(context.Background(), testShadowConfig(), time.Now()) {
		t.Fatal("「止めて」が効いている間は走らない")
	}
	if _, err := os.Stat(filepath.Join(dir, "shadow", "curriculum.jsonl")); err == nil {
		t.Fatal("走っていないのに記録が増えている")
	}
}

func TestShadowReadsProjectsFromProjectCr(t *testing.T) {
	// 読み先が Project CR に移ったこと (設計 state-out-of-git 4b-2a)。
	// 終端は selector で落ちるので、空きスロットの数え方は git 版と変わらない
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusNoContent,
		`{"projects":[{"state":"active"},{"state":"delivered"},{"state":"rejected"}],`+
			`"stop_engaged":false,"last_curriculum_at":"2026-08-24T09:00:00Z"}`)
	c := newShadowClient(t, s, dir)

	doc, err := c.fetchProjects(context.Background(), testShadowConfig())
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Projects) != 1 || doc.Projects[0].State != "active" {
		t.Fatalf("非終端だけを受け取るべき: %#v", doc.Projects)
	}
	if doc.LastCurriculumAt != "2026-08-24T09:00:00Z" || doc.StopEngaged {
		t.Fatalf("doc 全体の状態は heart の /healthz から来るべき: %#v", doc)
	}
	if len(s.kubeCalls) != 1 || !strings.Contains(s.kubeCalls[0], "lifecycle%3Dlive") {
		t.Fatalf("live selector で引くべき (終端 250 件超を毎回引かない): %v", s.kubeCalls)
	}
	if len(s.githubCalls) != 0 {
		t.Fatalf("状態を GitHub から読んではいけない: %v", s.githubCalls)
	}
}

func TestShadowSkipsWhenHeartStateIsUnreadable(t *testing.T) {
	// stop_engaged が読めないまま走ると、人間が「止めて」と言った後に
	// 立案がトークンを燃やす。読めないときは走らない (fail-closed)
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusNoContent, idleProjects)
	c := newShadowClient(t, s, dir)
	s.heart.Close()

	if c.maybeRunShadow(context.Background(), testShadowConfig(), time.Now()) {
		t.Fatal("heart の状態が読めないなら走らない")
	}
	if _, err := os.Stat(filepath.Join(dir, "shadow", "curriculum.jsonl")); err == nil {
		t.Fatal("走っていないのに記録が増えている")
	}
}

func TestShadowRecordsTheFailureInsteadOfRetrying(t *testing.T) {
	// 1 段目が返らないとき、黙って消えず「返らなかった」を記録に残すこと。
	// cursor も進めるので、次の周回で連打しない
	dir := t.TempDir()
	s := newShadowServers(t, http.StatusInternalServerError, idleProjects)
	c := newShadowClient(t, s, dir)

	cfg := testShadowConfig()
	now := time.Now()
	c.maybeRunShadow(context.Background(), cfg, now)

	rec := readLastShadowRecord(t, filepath.Join(dir, "shadow", "curriculum.jsonl"))
	if rec.Error == "" {
		t.Fatal("失敗を記録すべき")
	}
	if last := loadShadowCursor(c.shadowCursor()); last.IsZero() {
		t.Fatal("cursor を進めるべき (失敗しても連打しない)")
	}
	if c.maybeRunShadow(context.Background(), cfg, now.Add(time.Minute)) {
		t.Fatal("間隔が空いていないのに再実行している")
	}
}

func readLastShadowRecord(t *testing.T, path string) shadowRecord {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("記録が無い: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	var rec shadowRecord
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &rec); err != nil {
		t.Fatalf("記録が JSONL として壊れている: %v", err)
	}
	return rec
}

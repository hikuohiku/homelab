// 沈黙の見張り (設計 state-out-of-git Phase 7)。
//
// 器が黙ったことに気づいて所有者に言う役。以前は GitHub Actions で 30 分ごとに回る
// watchdog が ops-state ブランチを読み、issue #56 にコメントしていた。状態が git から
// 出るのに合わせて、この役をコアへ移した。コアは既に人間と話す口を持っているので、
// 新しい面は増えない。
//
// **見るのはビートの鮮度であって、プロセスの生死ではない。** P-0027 の事故は
// 「止まったまま死んだ」— プロセスは生きているのにループが回っていなかった。
// /healthz が 200 を返すことを見る実装はその事故を再現する。だから見るのは:
//
//	(1) heart の Lease の renewTime — ビートが最後まで通ったときだけ進む
//	(2) 健全性レポート ConfigMap の generated_at — reporter が実際に書いたときだけ進む
//
// どちらも「仕事の成果が更新された時刻」で、返事ができることでは進まない。
// (2) は Phase 5 で読み先が消えて以来、誰も見張っていなかった穴を塞ぐもの。
//
// **fail-closed**: 読めない / 壊れている / 時刻が無い → 沈黙とみなす。沈黙を検知する
// 道具が「読めなかった」を「元気」に倒したら存在意義が無い。
//
// **node01 ごと死んだらコアも死ぬ。** そのときは Telegram が応答しなくなる。所有者は
// Telegram で日常的に話しかけているので、沈黙は使っている経路の上で可視になる。
// 器の外に別の見張りは置かない (設計の原則 3)。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// 同じ検知を言い直すまでの間隔。旧 watchdog の REPOST_COOLDOWN_SECONDS と同じ 6h。
// rules.json ではなくここに置くのは、あちらが CODEOWNERS 保護パス (触ると
// auto-merge が止まる) だから — 旧 ops/check_heartbeat_fresh.py と同じ流儀。
// 検知が続いていることはログに残り続けるので、Telegram は「気づかせる」だけでよい。
const silenceRepostCooldown = 6 * time.Hour

// 見張りの周期。k8s API を 2 本叩くだけなので安い。閾値 (時間オーダー) に対して
// 十分細かく、旧 watchdog の 30 分ポーリングより速く気づく
func silenceCheckInterval() time.Duration {
	return time.Duration(envOrInt("CORE_SILENCE_SECONDS", 60)) * time.Second
}

func heartLeaseTarget() (namespace, name string) {
	return envOr("CORE_HEART_LEASE_NAMESPACE", "autopilot"),
		envOr("CORE_HEART_LEASE_NAME", "autopilot-heart")
}

// --- 閾値 (ops/rules.json が単一情報源) ---

// thresholds は「これ以上更新が空いたら沈黙」の秒数。
type thresholds struct {
	heartbeat time.Duration
	health    time.Duration
}

// loadThresholds は main の作業コピー (repo.go が保つ /data/repo) から
// ops/rules.json を読む。**コードにも env にも閾値を置かない** — 定義は git、
// という原則をそのまま守るため。
func loadThresholds(repoDir string) (thresholds, error) {
	var out thresholds
	raw, err := os.ReadFile(filepath.Join(repoDir, "ops", "rules.json"))
	if err != nil {
		return out, fmt.Errorf("rules.json を読めない: %w", err)
	}
	var doc struct {
		Heartbeat struct {
			StaleSeconds int `json:"stale_seconds"`
		} `json:"heartbeat"`
		Health struct {
			StaleSeconds int `json:"stale_seconds"`
		} `json:"health"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return out, fmt.Errorf("rules.json が JSON として壊れている: %w", err)
	}
	if doc.Heartbeat.StaleSeconds <= 0 || doc.Health.StaleSeconds <= 0 {
		return out, fmt.Errorf("rules.json に heartbeat/health の stale_seconds が無い")
	}
	out.heartbeat = time.Duration(doc.Heartbeat.StaleSeconds) * time.Second
	out.health = time.Duration(doc.Health.StaleSeconds) * time.Second
	return out, nil
}

// --- 判定 (純関数) ---

// finding は観測 1 本の判定結果。
type finding struct {
	name   string // 機械の鍵。cursor に載る
	what   string // 人間に見せる名前
	at     string // 観測した時刻 (読めなかったら空)
	age    time.Duration
	stale  bool
	reason string
}

// judgeFreshness は「最後に仕事をした時刻」1 本を判定する。
//
// fail-closed: readErr / at が空 / 解釈できない → stale。
// 未来の時刻 (clock skew) は沈黙ではないので fresh に倒し、理由に残す。
func judgeFreshness(name, what, at string, readErr error, now time.Time, threshold time.Duration) finding {
	f := finding{name: name, what: what, at: at}
	if readErr != nil {
		f.stale = true
		f.reason = fmt.Sprintf("%s を読めない (%v) — 読めないことは元気の証拠ではない (fail-closed)", what, readErr)
		return f
	}
	if strings.TrimSpace(at) == "" {
		f.stale = true
		f.reason = fmt.Sprintf("%s に時刻が無い — 書き手が壊れている可能性 (fail-closed)", what)
		return f
	}
	ts, err := parseStamp(at)
	if err != nil {
		f.stale = true
		f.reason = fmt.Sprintf("%s の時刻 %q を解釈できない (fail-closed)", what, at)
		return f
	}
	f.age = now.Sub(ts)
	if f.age < 0 {
		f.reason = fmt.Sprintf("%s の時刻が %s 未来にある (clock skew)。沈黙ではない", what, -f.age)
		return f
	}
	if f.age >= threshold {
		f.stale = true
		f.reason = fmt.Sprintf("%s が %s 更新されていない (閾値 %s)", what, roundAge(f.age), threshold)
		return f
	}
	f.reason = fmt.Sprintf("%s は %s 前に更新されている (閾値 %s)", what, roundAge(f.age), threshold)
	return f
}

// parseStamp は RFC3339 系の時刻を読む。heart は秒精度の "…Z"、k8s の MicroTime は
// 小数秒付きで返しうるので両方を受ける。
func parseStamp(s string) (time.Time, error) {
	s = strings.TrimSpace(s)
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339, "2006-01-02T15:04:05Z"} {
		if ts, err := time.Parse(layout, s); err == nil {
			return ts.UTC(), nil
		}
	}
	return time.Time{}, fmt.Errorf("解釈できない時刻: %q", s)
}

func roundAge(d time.Duration) time.Duration {
	if d >= time.Minute {
		return d.Round(time.Minute)
	}
	return d.Round(time.Second)
}

// staleNames は沈黙している観測の鍵を名前順で返す。
func staleNames(findings []finding) []string {
	out := []string{}
	for _, f := range findings {
		if f.stale {
			out = append(out, f.name)
		}
	}
	sort.Strings(out)
	return out
}

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// shouldAlert は「いま所有者に言うべきか」を決める (純関数)。
//
//   - 顔ぶれが変わったら必ず言う (新しい沈黙・回復は待たせない)
//   - 同じ沈黙が続いている間は cooldown ぶん黙る (6h ごとに 1 回だけ言い直す)
//   - 全部 fresh で、前回も何も無かったなら言わない
func shouldAlert(current, previous []string, lastAlert time.Time, now time.Time,
	cooldown time.Duration) (bool, string) {
	changed := !sameSet(current, previous)
	if len(current) == 0 {
		if changed {
			return true, "沈黙が解消した"
		}
		return false, "全部新しい"
	}
	if changed {
		return true, "沈黙の顔ぶれが変わった"
	}
	if lastAlert.IsZero() || now.Sub(lastAlert) >= cooldown {
		return true, fmt.Sprintf("同じ沈黙が続いている (前回の通知から %s 以上)", cooldown)
	}
	return false, "同じ沈黙を直近で通知済み"
}

// buildSilencePrompt はコアへの話しかけを組む。
//
// 本文は所有者へそのまま転送されるものではなく、コアが読んで自分の言葉で言う材料。
// 「直せる」と言わせないのは watchHealth と同じ理由で、コアに修理の手段が無いため。
func buildSilencePrompt(findings []finding, current, previous []string, now time.Time) string {
	var b strings.Builder
	if len(current) == 0 {
		b.WriteString("器の沈黙が解消した (人間からの依頼ではなく、定期観測による自発的な気づき)。\n\n")
	} else {
		b.WriteString("器が黙っている (人間からの依頼ではなく、定期観測による自発的な気づき)。\n\n")
	}
	fmt.Fprintf(&b, "判定時刻 (UTC): %s\n", now.UTC().Format(time.RFC3339))
	fmt.Fprintf(&b, "前回の沈黙: %s\n", orNone(previous))
	fmt.Fprintf(&b, "今回の沈黙: %s\n\n", orNone(current))
	for _, f := range findings {
		mark := "ok"
		if f.stale {
			mark = "沈黙"
		}
		fmt.Fprintf(&b, "- [%s] %s: %s\n", mark, f.what, f.reason)
	}
	b.WriteString("\n")
	if len(current) == 0 {
		b.WriteString("復旧したように見える。所有者に telegram_reply で一言だけ知らせること。")
		return b.String()
	}
	b.WriteString("見ているのは「最後に仕事をした時刻」であって、プロセスが生きているかではない。" +
		"プロセスが起きたまま止まっている状態もここに出る。" +
		"homelab_pods や homelab_events で周辺を確かめたうえで、" +
		"所有者に telegram_reply で知らせること。" +
		"直せるとは言わないこと — お前に修理の手段は無い。")
	return b.String()
}

// --- 見張り ---

type silenceWatcher struct {
	cursorPath string
	repoDir    string
	// 最後に読めた閾値。作業コピーの clone が終わる前は読めないので、
	// 一度読めた値を持ち回る (読めないことを理由に見張りを止めない)
	limits thresholds
	loaded bool
}

func newSilenceWatcher(cfg *config) *silenceWatcher {
	return &silenceWatcher{
		cursorPath: filepath.Join(cfg.stateDir, "silence-cursor.json"),
		repoDir:    repoWorkDir(cfg),
	}
}

type silenceCursor struct {
	Stale     []string `json:"stale"`
	AlertedAt string   `json:"alerted_at"`
}

func loadSilenceCursor(path string) silenceCursor {
	var c silenceCursor
	raw, err := os.ReadFile(path)
	if err != nil {
		return silenceCursor{}
	}
	if json.Unmarshal(raw, &c) != nil {
		return silenceCursor{}
	}
	if c.Stale == nil {
		c.Stale = []string{}
	}
	return c
}

// observe は 2 本の鮮度を読んで判定する。k8s に届かないこと自体も沈黙とみなす。
func (w *silenceWatcher) observe(ctx context.Context, c *client, now time.Time) []finding {
	k, kubeErr := c.kubeAPI()

	leaseAt, leaseErr := "", kubeErr
	if kubeErr == nil {
		leaseAt, leaseErr = k.heartLeaseRenewTime(ctx)
	}
	healthAt, healthErr := "", kubeErr
	if kubeErr == nil {
		healthAt, healthErr = k.healthReportGeneratedAt(ctx)
	}

	return []finding{
		judgeFreshness("heart", "heart のビート (Lease の renewTime)",
			leaseAt, leaseErr, now, w.limits.heartbeat),
		judgeFreshness("health-report", "健全性レポート (ConfigMap の generated_at)",
			healthAt, healthErr, now, w.limits.health),
	}
}

// tick は 1 周ぶんの見張り。所有者に言ったときだけ true を返す。
func (w *silenceWatcher) tick(ctx context.Context, c *client, sessionID string, now time.Time) bool {
	if limits, err := loadThresholds(w.repoDir); err == nil {
		w.limits, w.loaded = limits, true
	} else if !w.loaded {
		// 作業コピーがまだ無い起動直後はここに来る。閾値を勝手に決めない
		// (埋め込んだ既定で判定すると、rules.json を直しても効かない見張りになる)
		log.Printf("沈黙の判定を保留: %v", err)
		return false
	}

	findings := w.observe(ctx, c, now)
	current := staleNames(findings)
	prev := loadSilenceCursor(w.cursorPath)
	lastAlert, _ := parseStamp(prev.AlertedAt)

	alert, why := shouldAlert(current, prev.Stale, lastAlert, now, silenceRepostCooldown)
	if !alert {
		return false
	}

	if err := c.prompt(ctx, sessionID, buildSilencePrompt(findings, current, prev.Stale, now)); err != nil {
		// 言えなかったら cursor を進めない。次の周回でやり直す
		log.Printf("沈黙をコアへ渡せない (次の周回で再試行): %v", err)
		return false
	}
	if err := writeJSON(w.cursorPath, silenceCursor{
		Stale: current, AlertedAt: now.UTC().Format(time.RFC3339),
	}); err != nil {
		log.Printf("沈黙の cursor を保存できない: %v", err)
	}
	log.Printf("沈黙をコアへ渡した (%s): %s → %s", why, orNone(prev.Stale), orNone(current))
	return true
}

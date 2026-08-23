// heart-bus-sidecar — heart Pod に同居して、NATS のイベントを heart が読める
// ファイルに落とす係 (設計 docs/design/event-driven-core D16、移行の段階 3)。
//
// 何のためか: heart が人間の書き置き (「止めて」「veto P-NNNN」) を受け取る経路は
// GitHub (issue #56 と ops-feedback ブランチ) だけだった。所有者の緊急停止が第三者の
// 可用性に依存している。ここにクラスタ内で完結する第 2 の経路を足す。
//
//	telegram-adapter → NATS(events.raw.>) → [このプロセス] → /data/feedback-bus/inbox/<id>.json
//	                 → GitHub ops-feedback → heart の git 走査              ↓
//	                                                       heart の facts.collect_feedback()
//
// heart 本体 (Python) を Go へ書き換えないのは、reconcile の 200 超のテストを持つ
// 決定論の要を丸ごと触るのを避けるため。heart への受け渡しは「ファイルを置く」だけで、
// このプロセスは何も判断しない (triage は heart の純関数のまま)。
//
// 重複排除はここではやらない。同じ書き置きが GitHub 経路とバス経路の両方から来るが、
// 両者の鍵 (ファイル名 "<id>.json") が一致するので、heart の cursor
// (seen_feedback_files) が 1 つの集合で落とす。だからここは「同じ名前で置く」ことだけを
// 守ればよい。
//
// 守っている順序: ファイルを書く → fsync → ack。逆にすると、ack 直後に落ちたときに
// イベントが誰にも渡らないまま消える (誰も再送してくれない)。
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// busEvent は events.raw.<domain>.<source> に流れているイベント。
// telegram-adapter の busEvent / inbox note と同じ形。
type busEvent struct {
	ID       string `json:"id"`
	Source   string `json:"source"`
	Received string `json:"received"`
	Kind     string `json:"kind,omitempty"`
	Body     string `json:"body"`
	UpdateID int64  `json:"update_id,omitempty"`
}

// id は heart の cursor の鍵 (= ファイル名) になるので、パスとして安全な形だけ通す。
// telegram-adapter の noteID は "20060102-150405-<hex>" なのでこれに収まる。
var safeID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type config struct {
	outDir      string
	pollSeconds int
	retain      time.Duration
	fetchMax    int
}

func envOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	v, err := strconv.Atoi(envOr(key, ""))
	if err != nil || v <= 0 {
		return def
	}
	return v
}

func loadConfig() config {
	return config{
		// heart の HEART_FEEDBACK_BUS_DIR と同じ場所を指すこと。
		// 既定値を両側で揃えてあるので、通常はどちらも設定しなくてよい
		outDir: envOr("BUS_SIDECAR_OUT_DIR", "/data/feedback-bus/inbox"),
		// heart のビートは 60s。それより短く取っておけば、バスの取り込みが
		// ビートの遅れの主因にならない
		pollSeconds: envInt("BUS_SIDECAR_POLL_SECONDS", 5),
		// 置きっぱなしのファイルを掃除するまでの時間。heart の cursor が既読を
		// 覚えているので消してよい。EVENTS ストリームの保持期間と同じ 7 日
		retain:   time.Duration(envInt("BUS_SIDECAR_RETAIN_HOURS", 168)) * time.Hour,
		fetchMax: 16,
	}
}

// notePath はイベント id から書き出し先を返す。名前を "<id>.json" にするのは、
// heart の既読 cursor が GitHub 側の inbox ファイル名 (ops/feedback/inbox/<id>.json)
// で持っているため。ここを別の名前にすると 2 経路の重複が落とせない。
func notePath(dir, id string) (string, error) {
	id = strings.TrimSpace(id)
	if !safeID.MatchString(id) {
		return "", fmt.Errorf("イベント id がファイル名として使えない: %q", id)
	}
	return filepath.Join(dir, id+".json"), nil
}

// writeNote は note 1 件を atomically 置く。同名が既にあれば何もしない (冪等)。
//
// 一時ファイル → fsync → rename → ディレクトリ fsync の順。rename までは heart から
// 見えないので、書きかけの JSON を heart が読むことはない。ディレクトリまで fsync
// するのは、この後で ack して「もう再送されない」状態になるため。
func writeNote(dir string, e busEvent) (string, error) {
	path, err := notePath(dir, e.ID)
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(path); err == nil {
		return path, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}

	raw, err := json.Marshal(e)
	if err != nil {
		return "", err
	}
	raw = append(raw, '\n')

	// 一時ファイルは "." 始まりにする。heart 側の走査は "." 始まりを読まない
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) //nolint:errcheck // rename 成功後は存在しない

	if _, err := tmp.Write(raw); err != nil {
		tmp.Close() //nolint:errcheck // 書き込みに失敗した後の close の失敗は情報を足さない
		return "", err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close() //nolint:errcheck
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	// 0644: heart は同じ Pod の同じ uid で読む。書けるのはこのプロセスだけでよい
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return "", err
	}
	if err := os.Rename(tmpName, path); err != nil {
		return "", err
	}
	if d, err := os.Open(dir); err == nil {
		err = d.Sync()
		d.Close() //nolint:errcheck // 読み取り専用ハンドル
		if err != nil {
			return "", fmt.Errorf("ディレクトリを fsync できない: %w", err)
		}
	} else {
		return "", fmt.Errorf("ディレクトリを開けない: %w", err)
	}
	return path, nil
}

// consume は fetch 済みのイベントを 1 件ずつ書き出す。
//
// 書けなかったものは ack しない。AckWait 後に再配送され、次の周回で再試行になる。
// 「壊れている / id が無い / 本文が空」は再配送されても直らないので term で落とす。
// 本文が空のものを heart に渡さないのは、triage が分類できずに briefing を汚すため。
func consume(cfg config, msgs []busMessage) {
	for _, m := range msgs {
		var e busEvent
		if err := json.Unmarshal(m.data(), &e); err != nil {
			log.Printf("バスのイベントが JSON として壊れている (捨てる): %v", err)
			_ = m.term()
			continue
		}
		if strings.TrimSpace(e.Body) == "" {
			log.Printf("本文が空のイベント (捨てる): id=%q", e.ID)
			_ = m.term()
			continue
		}
		path, err := notePath(cfg.outDir, e.ID)
		if err != nil {
			// id 無し・危険な id は heart の既読の鍵にできない = 二重処理の芽
			log.Printf("%v (捨てる)", err)
			_ = m.term()
			continue
		}
		if _, err := writeNote(cfg.outDir, e); err != nil {
			log.Printf("%s を書けない (ack しないので再配送される): %v", filepath.Base(path), err)
			continue
		}
		// 書けてから ack する。逆順にすると落ちたときにイベントが消える
		if err := m.ack(); err != nil {
			// 再配送されても writeNote が冪等なので実害は無いが、黙らせない
			log.Printf("%s: ack に失敗 (再配送されても同じ名前で上書きしない): %v",
				filepath.Base(path), err)
		}
		log.Printf("heart へ渡した: %s (%s, %d chars%s)",
			filepath.Base(path), e.Source, len(e.Body), lagSuffix(e.Received))
	}
}

// prune は retain を過ぎたファイルを消す。heart の cursor が既読を覚えているので、
// 消しても読み直されない。掃除しないと共有ボリュームが単調に増える。
func prune(dir string, retain time.Duration, now time.Time) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	removed := 0
	for _, ent := range entries {
		name := ent.Name()
		if ent.IsDir() {
			continue
		}
		// 走り書きの一時ファイルも同じ基準で掃除する (異常終了の残骸)
		if !strings.HasSuffix(name, ".json") && !strings.HasPrefix(name, ".tmp-") {
			continue
		}
		info, err := ent.Info()
		if err != nil {
			continue
		}
		if now.Sub(info.ModTime()) < retain {
			continue
		}
		if err := os.Remove(filepath.Join(dir, name)); err == nil {
			removed++
		}
	}
	return removed
}

// lagSuffix は書き置きの受信時刻から今までの遅れを返す。
// 「反応が遅い」を体感でなく数字で見るための計測点 (core-driver と同型)。
func lagSuffix(received string) string {
	if received == "" {
		return ""
	}
	at, err := time.Parse("2006-01-02T15:04:05Z", received)
	if err != nil {
		return ""
	}
	return fmt.Sprintf(", lag %ds", int(time.Since(at).Seconds()))
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	cfg := loadConfig()

	if err := os.MkdirAll(cfg.outDir, 0o755); err != nil {
		// ここで落ちても heart は GitHub 経路で動き続ける。restartPolicy が
		// 立て直すので、黙って劣化運転を続けるより落ちて気づかせる方がよい
		log.Fatalf("書き出し先 %s を作れない: %v", cfg.outDir, err)
	}
	log.Printf("heart-bus-sidecar 起動 (out=%s poll=%ds retain=%s)",
		cfg.outDir, cfg.pollSeconds, cfg.retain)

	bus := connectBusOrLog()
	// bus は再接続で差し替わるので、defer 時点の値を捕まえないよう closure で包む
	defer func() { bus.close() }()

	lastRetry := time.Now()
	lastPrune := time.Time{}
	for {
		if bus == nil && time.Since(lastRetry) >= busRetryInterval {
			lastRetry = time.Now()
			// 未設定なら (nil, nil) が返るだけなので、この再試行は何もしない
			bus = connectBusOrLog()
		}
		paced := false
		if bus != nil {
			msgs, err := bus.fetch(cfg.fetchMax, time.Duration(cfg.pollSeconds)*time.Second)
			if err != nil {
				// 繋がらない間も heart の GitHub 経路は生きている。止めずにログだけ残す
				log.Printf("バスから読めない (継続): %v", err)
			} else {
				paced = true
				consume(cfg, msgs)
			}
		}
		if time.Since(lastPrune) >= time.Hour {
			lastPrune = time.Now()
			if n := prune(cfg.outDir, cfg.retain, time.Now()); n > 0 {
				log.Printf("古い書き置きを %d 件掃除した", n)
			}
		}
		if !paced {
			time.Sleep(time.Duration(cfg.pollSeconds) * time.Second)
		}
	}
}

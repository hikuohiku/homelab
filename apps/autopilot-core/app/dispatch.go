// NATS への publish。コアが heart に仕事を頼むための唯一の出口 (設計 D3/D7)。
//
// コアは git にも K8s にも書かない。実装依頼 (task-request) は
// events.heart.<domain>.<command> へ publish するだけで、ops-state への commit も
// Job の spawn も heart が行う。単一書き手の不変条件と、並列上限・監査の
// 一元化をここで壊さないため。
//
// 失敗がどう返るか (実装前に実サーバで確かめた点):
//   - core NATS の Publish は fire-and-forget で、**ACL 違反でもエラーを返さない**。
//     だから JetStream publish (server の ack を待つ) を使う。権限違反は
//     "Permissions Violation" 由来の応答無し = timeout として呼び出し側に返る
//   - publish 先を captured するストリームが無ければ "no responders" で失敗する。
//     「送ったつもり」でログに成功と出る事態を避けられる
//
// 鍵は core 専用の NKey (NATS_CORE_NKEY_SEED)。telegram-adapter の producer 鍵を
// 使い回さないのは、事故のときに「誰が流したか」を権限で切り分けられるようにするため。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// commandTypeTaskRequest は heart のタスク依頼キュー (ops/heart/tasks.py の
// KIND_TASK_REQUEST) に載せる command の種別。値を変えると heart 側の
// 取り込みが黙って止まるので、両側を同時に変えること。
const commandTypeTaskRequest = "task-request"

const (
	// 題名は heart の briefing / curriculum プロンプトに 1 行として載る長さ
	maxCommandTitleRunes = 120
	// 本文は curriculum が読む原料。長すぎると tasks.for_env が切るので、
	// 切られる前にコアへ「長すぎる」と返す方が正直
	maxCommandBodyRunes = 4000
)

// commandEvent は events.heart.<domain>.<command> に流すコマンド。
// heart 側 (bus-sidecar → ops/heart/facts.collect_commands) がこの形を読む。
type commandEvent struct {
	// CommandID は処理済み台帳の鍵。内容から決定論的に導く (commandID を参照)
	CommandID string `json:"command_id"`
	Type      string `json:"type"`
	Source    string `json:"source"`
	IssuedAt  string `json:"issued_at"`
	Title     string `json:"title,omitempty"`
	Body      string `json:"body"`
}

// commandID は command の id を内容から導く。
//
// 時刻や乱数を混ぜないのは、再送 (publish の失敗をコアが見て呼び直す / JetStream の
// 再配送) で同じ依頼が 2 件のプロジェクトになるのを防ぐため。同じ本文の依頼は
// 何度出しても 1 件として扱われる — 取りこぼしより二重着手の方が高くつく。
func commandID(kind, title, body string) string {
	sum := sha256.Sum256([]byte(kind + "\x00" + title + "\x00" + body))
	return "core-" + hex.EncodeToString(sum[:])[:16]
}

// newTaskRequest は引数を検証して task-request の command を組む。
// 検証に落ちたら送らない (空の依頼を heart のキューに積まない)。
func newTaskRequest(title, body string, now time.Time) (commandEvent, error) {
	title = strings.TrimSpace(title)
	body = strings.TrimSpace(body)
	if title == "" {
		return commandEvent{}, errors.New("title が空。何の依頼か 1 行で書くこと")
	}
	if body == "" {
		return commandEvent{}, errors.New("body が空。何をどうしたいかを書くこと")
	}
	if n := len([]rune(title)); n > maxCommandTitleRunes {
		return commandEvent{}, fmt.Errorf("title が長すぎる (%d 文字 > %d)", n, maxCommandTitleRunes)
	}
	if n := len([]rune(body)); n > maxCommandBodyRunes {
		return commandEvent{}, fmt.Errorf("body が長すぎる (%d 文字 > %d)。要点に絞ること", n, maxCommandBodyRunes)
	}
	return commandEvent{
		CommandID: commandID(commandTypeTaskRequest, title, body),
		Type:      commandTypeTaskRequest,
		Source:    "core",
		IssuedAt:  now.UTC().Format("2006-01-02T15:04:05Z"),
		Title:     title,
		Body:      body,
	}, nil
}

type busPublisher struct {
	conn          *nats.Conn
	js            nats.JetStreamContext
	subjectPrefix string
}

// connectPublisher は core 専用の NKey で NATS に繋ぐ。設定が無ければ (nil, nil) —
// バスを使わない構成 (切り戻し) でも MCP サーバ自体は起動でき、request_task だけが
// 「経路が無い」と isError で返る。
func connectPublisher() (*busPublisher, error) {
	url := strings.TrimSpace(os.Getenv("NATS_URL"))
	seed := strings.TrimSpace(os.Getenv("NATS_NKEY_SEED"))
	if url == "" || seed == "" {
		return nil, nil
	}

	// seed はファイルに書かずメモリ上で署名する (driver の bus.go と同じ)
	kp, err := nkeys.FromSeed([]byte(seed))
	if err != nil {
		return nil, fmt.Errorf("NKey seed を読めない: %w", err)
	}
	pub, err := kp.PublicKey()
	if err != nil {
		return nil, fmt.Errorf("NKey の公開鍵を導けない: %w", err)
	}

	conn, err := nats.Connect(url,
		nats.Nkey(pub, kp.Sign),
		nats.Name("core-mcp"),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
	)
	if err != nil {
		return nil, err
	}
	js, err := conn.JetStream()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("JetStream を使えない: %w", err)
	}
	return &busPublisher{
		conn: conn, js: js,
		subjectPrefix: strings.TrimSuffix(
			envOr("NATS_COMMAND_SUBJECT_PREFIX", "events.heart.homelab"), "."),
	}, nil
}

// publish は command を 1 件流し、server の ack を待つ。
//
// Nats-Msg-Id に command_id を使うので、dupe window 内の再送はストリーム側でも
// 落ちる (heart 側の台帳と二重の守り)。
func (b *busPublisher) publish(e commandEvent) error {
	raw, err := json.Marshal(e)
	if err != nil {
		return err
	}
	subject := b.subjectPrefix + "." + e.Type
	_, err = b.js.Publish(subject, raw,
		nats.MsgId(e.CommandID),
		nats.AckWait(5*time.Second))
	if err != nil {
		return fmt.Errorf("%s へ流せない: %w", subject, err)
	}
	return nil
}

func (b *busPublisher) close() {
	if b != nil && b.conn != nil {
		b.conn.Drain() //nolint:errcheck // 終了時のみ。失敗しても他にできることが無い
	}
}

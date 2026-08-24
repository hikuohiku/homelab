// NATS への publish。書き置きの唯一の出口。
//
// 以前は GitHub (ops-feedback ブランチ) との両書きだった。状態が git から出るのに
// 合わせて閉じた (設計 state-out-of-git Phase 7)。
//
// 失敗の扱い: 冗長側が無くなったので、publish が通るまで Telegram の offset を
// 進めない (呼び出し側 runAdapter)。「送れなかったが受信済み」にすると書き置きが
// そのまま消える。設定が無いときも起動しない (下記 connectBus)。
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// busEvent は events.raw.<domain>.<source> に流すイベント。
// inbox note と同じ形にしてあるので、consumer 側は出所で形を変えなくてよい。
type busEvent struct {
	ID       string `json:"id"`
	Source   string `json:"source"`
	Received string `json:"received"`
	Kind     string `json:"kind,omitempty"`
	Body     string `json:"body"`
	// UpdateID は Telegram の update_id。全経路の冪等キー (D26)
	UpdateID int64 `json:"update_id,omitempty"`
}

type busPublisher struct {
	conn    *nats.Conn
	js      nats.JetStreamContext
	subject string
}

// connectBus は NKey で NATS に繋ぐ。**設定が無ければエラー**。
// 出口が 1 本になったので、バス無しで動く構成はもう存在しない。
func connectBus() (*busPublisher, error) {
	url := strings.TrimSpace(os.Getenv("NATS_URL"))
	seed := strings.TrimSpace(os.Getenv("NATS_NKEY_SEED"))
	if url == "" || seed == "" {
		return nil, fmt.Errorf("NATS_URL / NATS_NKEY_SEED が無い (書き置きの出口が無い)")
	}

	// seed をファイルに書かずメモリ上で署名する。nats.NkeyOptionFromSeed は
	// ファイルパスを取る API だが、このコンテナは readOnlyRootFilesystem なので
	// 秘密を書ける場所が無い (書けたとしても、書かないに越したことはない)
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
		nats.Name("telegram-adapter"),
		// 落ちても諦めない。再接続は無限に試み、その間の publish は失敗として扱う
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Printf("NATS 切断 (再接続を試みる): %v", err)
		}),
		nats.ReconnectHandler(func(c *nats.Conn) {
			log.Printf("NATS 再接続: %s", c.ConnectedUrl())
		}),
	)
	if err != nil {
		return nil, err
	}

	// JetStream の publish を使う。core NATS の Publish は fire-and-forget で、
	// 権限違反もストリーム不在も**エラーとして返らない** (2026-08-23 実測)。
	// それだと「送ったつもり」でログに成功と出てしまう。JetStream なら
	// server の ack を待つので、届いていないことが呼び出し側に伝わる
	js, err := conn.JetStream()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("JetStream を使えない: %w", err)
	}

	subject := envOr("NATS_SUBJECT", "events.raw.homelab.telegram")
	return &busPublisher{conn: conn, js: js, subject: subject}, nil
}

func (b *busPublisher) publish(e busEvent) error {
	raw, err := json.Marshal(e)
	if err != nil {
		return err
	}
	// ack を待つ。ストリームが無ければ「no responders」で失敗するので、
	// 保存先が用意される前に流して消える、という事態を検知できる。
	// Nats-Msg-Id を付けると JetStream 側が重複を落とす (dupe window 内)。
	// update_id は全経路の冪等キー (D26) なのでそれを使う
	_, err = b.js.Publish(b.subject, raw,
		nats.MsgId(fmt.Sprintf("telegram-%d", e.UpdateID)),
		nats.AckWait(5*time.Second))
	return err
}

func (b *busPublisher) close() {
	if b != nil && b.conn != nil {
		b.conn.Drain() //nolint:errcheck // 終了時のみ。失敗しても他にできることが無い
	}
}

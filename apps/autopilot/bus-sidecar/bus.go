// NATS からの consume。heart の第 2 の入力口 (設計 D16、移行の段階 3)。
//
// autopilot-core の bus.go と同型で、違うのは durable 名とクライアント名だけ。
// 同じストリームを別 durable で読むので、コアと heart は互いの読み位置を動かさない。
//
// 失敗がどう返るか (core 側で実サーバに当てて確かめた事実。ここでも同じ):
//   - core NATS の Publish / Msg.Ack は fire-and-forget で、権限違反でもエラーを
//     返さない。ack は AckSync (server の応答を待つ) を使う。ACL には $JS.ACK.> が要る
//   - Fetch は「今回は何も無かった」を nats.ErrTimeout で返す。これは異常ではない
//   - PullSubscribe / Fetch はストリーム不在・権限不足を error として返す
package main

import (
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// バスに繋げなかったときの再試行間隔。GitHub 経路が生きている間の劣化運転なので
// 急がないが、繋がったことに気づかないまま何時間も片肺で走るほど間を空けない。
const busRetryInterval = time.Minute

// connectBusOrLog は connectBus の結果をログにして返す。繋げなければ nil。
// 「未設定 (意図した切り戻し)」と「繋げない (異常)」をログで区別する。
func connectBusOrLog() *busConsumer {
	bus, err := connectBus()
	if err != nil {
		log.Printf("バスに繋げない (heart は GitHub 経路で動き続ける。%s 後に再試行): %v", busRetryInterval, err)
		return nil
	}
	if bus == nil {
		return nil
	}
	log.Printf("バスに接続 (stream=%s durable=%s filter=%s)", bus.stream, bus.durable, bus.filter)
	return bus
}

// busMessage は「バスから来た 1 件」。実体は nats.Msg だが、書き出しと ack の順序を
// 実サーバ無しで検証できるよう interface で挟む。
type busMessage interface {
	data() []byte
	// ack は「受け取って永続化し終えた」を server に伝える。書く前に呼んではいけない
	ack() error
	// term は「二度と配送しなくてよい」。壊れたイベントを無限に再配送させないため
	term() error
}

type natsMessage struct{ msg *nats.Msg }

func (m natsMessage) data() []byte { return m.msg.Data }

// AckSync は server の応答を待つ。Ack (非同期) だと権限違反すら nil で返る
func (m natsMessage) ack() error  { return m.msg.AckSync(nats.AckWait(5 * time.Second)) }
func (m natsMessage) term() error { return m.msg.Term() }

type busConsumer struct {
	conn    *nats.Conn
	sub     *nats.Subscription
	stream  string
	durable string
	filter  string
}

// connectBus は NKey で NATS に繋ぎ、durable pull consumer を張る。
// 設定が無ければ (nil, nil) を返す — バスを使わない構成 (切り戻し) でも
// heart が GitHub 側だけで動けるようにしておく。
func connectBus() (*busConsumer, error) {
	url := strings.TrimSpace(os.Getenv("NATS_URL"))
	seed := strings.TrimSpace(os.Getenv("NATS_NKEY_SEED"))
	if url == "" || seed == "" {
		return nil, nil
	}

	// seed はファイルに書かずメモリ上で署名する。このコンテナは
	// readOnlyRootFilesystem で書ける場所が共有ボリュームしかなく、
	// 書けたとしても秘密をディスクに落とす理由が無い
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
		nats.Name("heart-bus-sidecar"),
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

	js, err := conn.JetStream()
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("JetStream を使えない: %w", err)
	}

	stream := envOr("NATS_STREAM", "EVENTS")
	// コア (core-driver) とは別の durable。同じイベントを両者が独立に読む
	durable := envOr("NATS_DURABLE", "heart-feedback")
	filter := envOr("NATS_FILTER_SUBJECT", "events.raw.>")

	// durable にするのは、再起動で位置が巻き戻らない/飛ばないようにするため。
	// consumer の状態は server 側に残るので、Pod が入れ替わっても続きから読む。
	//
	// DeliverNew: consumer を**作るとき**の開始位置だけを決める。DeliverAll にすると
	// 初回作成時にストリームに残るぶんを再生してしまい、過去の「止めて」が今の
	// 停止として効いてしまう。作成前のぶんは heart の GitHub 経路が拾う
	sub, err := js.PullSubscribe(filter, durable,
		nats.BindStream(stream),
		nats.ManualAck(),
		nats.AckExplicit(),
		nats.DeliverNew(),
		// 書き出しに手間取っても、その間に再配送されないだけの猶予を取る。
		// 再配送されても heart の cursor が 1 回に落とすが、無駄な往復はしない
		nats.AckWait(2*time.Minute),
		nats.MaxAckPending(64),
	)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("consumer %s を張れない: %w", durable, err)
	}

	return &busConsumer{conn: conn, sub: sub, stream: stream, durable: durable, filter: filter}, nil
}

// fetch は最大 max 件を取りに行く。何も無ければ (nil, nil)。
// wait の間はブロックするので、呼ぶ側のループの間合いを兼ねる。
func (b *busConsumer) fetch(max int, wait time.Duration) ([]busMessage, error) {
	msgs, err := b.sub.Fetch(max, nats.MaxWait(wait))
	if err != nil {
		// 「今回は何も無かった」は異常ではない
		if errors.Is(err, nats.ErrTimeout) {
			return nil, nil
		}
		return nil, err
	}
	out := make([]busMessage, 0, len(msgs))
	for _, m := range msgs {
		out = append(out, natsMessage{msg: m})
	}
	return out, nil
}

func (b *busConsumer) close() {
	if b != nil && b.conn != nil {
		b.conn.Drain() //nolint:errcheck // 終了時のみ。失敗しても他にできることが無い
	}
}

// NATS からの consume。driver のもう 1 本の入力口 (設計 D16)。
//
// なぜ両読みか: telegram-adapter は同じ書き置きを GitHub (ops-feedback の inbox) と
// NATS の両方へ書いている。移行の途中で片方だけを読むと取りこぼすので、consumer も
// 両方から読む。GitHub 側を落とすのは shadow 並走で NATS 経路が確かめられてから。
//
// 両方から同じものが来る以上、重複を落とすのは driver の責務になる。鍵はイベントの
// id で、これは inbox のファイル名の語幹と同じ値 (telegram-adapter の noteID)。
// 既存の cursor (/data/cursor.json) がファイル名で既読を持っているので、そちらの形に
// 寄せて 1 つの集合で見る (seenKey)。コアが同じ書き置きに 2 回返事をしたら失敗。
//
// 失敗がどう返るか (実装前に確かめた点):
//   - core NATS の Publish は権限違反でもエラーを返さない。だから JetStream を使う
//     (telegram-adapter の bus.go と同じ判断)。consume 側も $JS.API 越しなので、
//     ストリーム不在・権限不足は PullSubscribe / Fetch の error として返る
//   - Fetch は「今回は何も無かった」を nats.ErrTimeout で返す。これは異常ではないので
//     エラー扱いしない。ここを取り違えるとログが埋まる
//   - **Msg.Ack() は fire-and-forget で、権限違反でも nil を返す**。実サーバで
//     確かめたら、ACL に $JS.ACK.> が無い状態でも ack が成功に見えていた
//     (メッセージは AckWait ごとに永久に再配送される)。だから AckSync を使う。
//     こちらは server の応答を待つので、届いていないことが呼び出し側に伝わる
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
		log.Printf("バスに繋げない (GitHub 側だけで動く。%s 後に再試行): %v", busRetryInterval, err)
		return nil
	}
	if bus == nil {
		return nil
	}
	log.Printf("バスに接続 (stream=%s durable=%s filter=%s)", bus.stream, bus.durable, bus.filter)
	return bus
}

// busMessage は「バスから来た 1 件」。実体は nats.Msg だが、driver 側の処理
// (重複排除と ack の順序) を実サーバ無しで検証できるよう interface で挟む。
type busMessage interface {
	data() []byte
	// ack は「コアへ渡し終えた」を server に伝える。渡す前に呼んではいけない
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
// driver が GitHub 側だけで動けるようにしておく。
func connectBus() (*busConsumer, error) {
	url := strings.TrimSpace(os.Getenv("NATS_URL"))
	seed := strings.TrimSpace(os.Getenv("NATS_NKEY_SEED"))
	if url == "" || seed == "" {
		return nil, nil
	}

	// seed はファイルに書かずメモリ上で署名する。driver は readOnlyRootFilesystem で
	// 書ける場所が無く、書けたとしても秘密をディスクに落とす理由が無い
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
		nats.Name("core-driver"),
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
	durable := envOr("NATS_DURABLE", "core-driver")
	filter := envOr("NATS_FILTER_SUBJECT", "events.raw.>")

	// durable にするのは、再起動で位置が巻き戻らない/飛ばないようにするため。
	// consumer の状態は server 側に残るので、Pod が入れ替わっても続きから読む。
	//
	// DeliverNew: consumer を**作るとき**の開始位置だけを決める。ここを DeliverAll に
	// すると、初回作成時にストリームに残る最大 7 日分を再生してしまう
	// (cursor が効くので二重返信にはならないが、起動直後に無駄な処理が走る)。
	// 作成前に publish されたぶんは GitHub 側のポーリングが拾うので落ちない。
	sub, err := js.PullSubscribe(filter, durable,
		nats.BindStream(stream),
		nats.ManualAck(),
		nats.AckExplicit(),
		nats.DeliverNew(),
		// コアへ渡すまでに時間がかかっても、その間に再配送されないだけの猶予を取る。
		// 再配送されても重複排除で 1 回に落ちるが、無駄な往復はしない方がよい
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

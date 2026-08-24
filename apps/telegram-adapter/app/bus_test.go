// バス publish の契約を固定する。
//
// NATS_TEST_URL と NATS_TEST_SEED が指されているときだけ実サーバに繋ぐ統合テストを走らせる。
// CI では起動しない（NATS を CI に立てるより、実機に近い検証は手元でやる方が確か）。
// 設定が無いときの挙動（バス無しで動く）は常に検証する。
package main

import (
	"os"
	"testing"
)

func TestConnectBusIsRequired(t *testing.T) {
	// 出口はここ 1 本 (設計 state-out-of-git Phase 7)。未設定で起動すると、
	// 受信した書き置きを黙って捨てながら ack だけ返し続けることになる
	t.Setenv("NATS_URL", "")
	t.Setenv("NATS_NKEY_SEED", "")

	bus, err := connectBus()
	if err == nil {
		t.Fatal("未設定は起動時に落とすべき")
	}
	if bus != nil {
		t.Fatal("失敗したら nil を返すべき")
	}
	// nil に対して close を呼んでも落ちないこと (defer で無条件に呼ぶため)
	bus.close()
}

func TestConnectBusRejectsBrokenSeed(t *testing.T) {
	// 壊れた seed を黙って無視すると、publish しないまま動き続ける。
	// 起動時に落として気づけるようにする
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("NATS_NKEY_SEED", "not-a-seed")

	if _, err := connectBus(); err == nil {
		t.Fatal("壊れた seed は起動時に弾くべき")
	}
}

// --- 実サーバ相手の統合 (NATS_TEST_URL があるときだけ) ---

func TestPublishAgainstRealServer(t *testing.T) {
	url := os.Getenv("NATS_TEST_URL")
	seed := os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)

	bus, err := connectBus()
	if err != nil {
		t.Fatalf("接続できない: %v", err)
	}
	if bus == nil {
		t.Fatal("設定があるのに nil")
	}
	defer bus.close()

	err = bus.publish(busEvent{
		ID: "20260823-000000-0000ff", Source: "telegram",
		Received: "2026-08-23T00:00:00Z", Body: "統合テスト", UpdateID: 255,
	})
	if err != nil {
		t.Fatalf("publish に失敗: %v", err)
	}
}

func TestPublishToForbiddenSubjectFails(t *testing.T) {
	// producer は events.> 以外に publish できない (ACL)。
	// ここが通ってしまうと権限設計が効いていない
	url := os.Getenv("NATS_TEST_URL")
	seed := os.Getenv("NATS_TEST_SEED")
	if url == "" || seed == "" {
		t.Skip("NATS_TEST_URL / NATS_TEST_SEED が無いので skip")
	}
	t.Setenv("NATS_URL", url)
	t.Setenv("NATS_NKEY_SEED", seed)
	t.Setenv("NATS_SUBJECT", "forbidden.subject")

	bus, err := connectBus()
	if err != nil {
		t.Fatalf("接続できない: %v", err)
	}
	defer bus.close()

	if err := bus.publish(busEvent{ID: "x", Source: "telegram", Body: "x"}); err == nil {
		t.Fatal("許可されていない subject への publish は失敗すべき")
	}
}

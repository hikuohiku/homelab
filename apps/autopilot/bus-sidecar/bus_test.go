package main

import (
	"testing"

	"github.com/nats-io/nkeys"
)

func TestConnectBusUnconfiguredIsNotAnError(t *testing.T) {
	// バス未設定は「意図した切り戻し」であって異常ではない。ここでエラーを返すと
	// 起動が落ち、GitHub 経路だけの構成に戻せなくなる
	t.Setenv("NATS_URL", "")
	t.Setenv("NATS_NKEY_SEED", "")
	bus, err := connectBus()
	if err != nil || bus != nil {
		t.Fatalf("未設定なら (nil, nil): bus=%v err=%v", bus, err)
	}
}

func TestConnectBusRejectsBrokenSeed(t *testing.T) {
	// 壊れた seed は「繋げない」として返る。黙って無認証で繋ぎにいかない
	t.Setenv("NATS_URL", "nats://127.0.0.1:14222")
	t.Setenv("NATS_NKEY_SEED", "これは seed ではない")
	bus, err := connectBus()
	if err == nil || bus != nil {
		t.Fatalf("壊れた seed が通っている: bus=%v err=%v", bus, err)
	}
}

func TestConnectBusOrLogSurvivesUnreachableServer(t *testing.T) {
	// 繋げなくても落ちないこと。停止経路を運ぶ側が可用性を下げてはいけない。
	// seed は形式として正しい捨て鍵をその場で作る (実サーバのどのユーザとも対応しない)
	kp, err := nkeys.CreateUser()
	if err != nil {
		t.Fatal(err)
	}
	seed, err := kp.Seed()
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("NATS_URL", "nats://127.0.0.1:1")
	t.Setenv("NATS_NKEY_SEED", string(seed))
	if bus := connectBusOrLog(connectBus); bus != nil {
		t.Fatal("繋がらないのに consumer が返っている")
	}
	if bus := connectBusOrLog(connectCommandBus); bus != nil {
		t.Fatal("繋がらないのに command consumer が返っている")
	}
}

func TestCommandBusUsesItsOwnDurable(t *testing.T) {
	// 書き置きと同じ durable にすると、片方が読んだぶんをもう片方が読めなくなる
	t.Setenv("NATS_URL", "")
	t.Setenv("NATS_NKEY_SEED", "")
	t.Setenv("NATS_DURABLE", "")
	t.Setenv("NATS_COMMAND_DURABLE", "")
	if envOr("NATS_DURABLE", "heart-feedback") == envOr("NATS_COMMAND_DURABLE", "heart-command") {
		t.Fatal("durable が同じでは 2 経路にならない")
	}
	if envOr("NATS_FILTER_SUBJECT", "events.raw.>") == envOr("NATS_COMMAND_FILTER_SUBJECT", "events.heart.>") {
		t.Fatal("filter が同じでは分流にならない")
	}
}

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
	if bus := connectBusOrLog(); bus != nil {
		t.Fatal("繋がらないのに consumer が返っている")
	}
}

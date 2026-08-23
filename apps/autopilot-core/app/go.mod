// 依存を足すときは ops/inventory.json への登録も併せて行うこと。
// nats.go / nkeys はイベント経路 (設計 D16) の consume 側のために入れた外部依存。
module github.com/hikuohiku/homelab/apps/autopilot-core

go 1.25.0

require (
	github.com/nats-io/nats.go v1.53.1
	github.com/nats-io/nkeys v0.4.15
)

require (
	github.com/klauspost/compress v1.18.5 // indirect
	github.com/nats-io/nuid v1.0.1 // indirect
	golang.org/x/crypto v0.49.0 // indirect
	golang.org/x/sys v0.42.0 // indirect
)

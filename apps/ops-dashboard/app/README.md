# Mission Control

Next.js server が Kubernetes API (Project CR / Lease / Job) と `autopilot-data` PVC、
heart の `/healthz` を読み、自律エージェントの状態と transcript を read-only で表示する。
**git は一切読まない** (設計 docs/design/state-out-of-git 4b-2b)。

```sh
npm ci
npm test
npm run build
```

本番では `HEART_DATA_DIR=/data`、`AUTOPILOT_NAMESPACE=autopilot` を使う。
`OPS_STATE_DIR` を指定すると、クラスタの代わりに fixture ディレクトリ
(`projects.json` / `heartbeat.json`) を読む。

初回リリースは二段階。アプリを main へ入れて SHA tag を build した後、その OCI index
digest を実測し、Deployment の image を digest pin する別 PR で旧静的版と入れ替える。

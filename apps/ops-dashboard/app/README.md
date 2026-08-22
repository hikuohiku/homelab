# Mission Control

Next.js server が Kubernetes API、`autopilot-data` PVC、`ops-state` branch を読み、
自律エージェントの状態と transcript を read-only で表示する。

```sh
npm ci
npm test
npm run build
```

本番では `HEART_DATA_DIR=/data`、`AUTOPILOT_NAMESPACE=autopilot` を使う。
`OPS_STATE_DIR` を指定すると git fetch の代わりに fixture ディレクトリを読める。

初回リリースは二段階。アプリを main へ入れて SHA tag を build した後、その OCI index
digest を実測し、Deployment の image を digest pin する別 PR で旧静的版と入れ替える。

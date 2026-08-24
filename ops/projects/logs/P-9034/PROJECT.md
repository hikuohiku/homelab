# P-9034 — 『Healthy なのに届かない』を最初の 1 時間で捕まえる — 全アプリの Service (clusterIP / tailnet) 到達性マトリクスを常設する

## 目的

critic 2026-08-24 の利用者所見 4 への直接応答: adguard が 242 回 CrashLoopBackOff を繰り返し
tailnet DNS が全停しても、ArgoCD の health は Deployment/Job の失敗しか見ないため
「人間が実際に叩く入口 (Service / tailnet) が応答するか」が 2 日間誰にも届かなかった。
P-0036 (observability 枠で棄却) を k8s 本体の計器に組み替え、クラスタ Service の dual-path
(clusterIP と MagicDNS) 到達性を baseline + CI 検査で常設し、壊れたことに人間より先に気づける
状態にする。画面の変更ではないため observability の枠を跨がない (VISION: 壊れたことに先に気づける)。

## 受入チェックリスト

initializer が 2026-08-24 に `project/p-9034` checkout のリポジトリルートから実行した結果、
**3 項目とも現時点で failing** (計器・テスト・baseline は未作成)。

- [ ] `test -f ops/tools/reachability_probe.py`
  — 各アプリの Service を clusterIP 経由 / tailnet 公開アプリは MagicDNS 経由で叩き、
  `ops/health/reachability.json` に結果を出すツール本体が実在すること。
  実測 rc=1 (ファイル未存在)。
- [ ] `python3 -m pytest ops/tests/test_reachability_probe.py -q`
  — adguard の「DNS 死」を fixture で再現する unittest が通り、計器がその失敗モードを
  捕まえられること。実測 rc=1 (テストファイル未存在。なおこの sandbox には pytest モジュールが
  入っておらず `No module named pytest` — verify の実施環境によっては worker 側の工夫が要る、
  設計方針参照)。
- [ ] `python3 ops/tools/reachability_probe.py --selftest`
  — ツールが自前の fixture (network-free) で自己検証でき、クラスタ実機なしでも
  判定ロジックの健全性を機械検査できること。実測 rc=1 (ファイル未存在)。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、
(1) `ops/tools/reachability_probe.py` (clusterIP + MagicDNS の dual-path、結果を
`ops/health/reachability.json` へ)、
(2) adguard の「DNS 死」を再現する fixture の unittest + `--selftest`、
(3) 今日の応答マップ (baseline) を docs に残す、までが本体。

## 設計方針

### 前提 (initializer が 2026-08-24 に実読・実測した。調べ直さなくてよい)

- **計器は k8s 本体の read-only ツール。** worker セッションはクラスタ内 (autopilot Pod、node01、
  tailnet メンバー) で動くため、`python3 ops/tools/reachability_probe.py` をセッションから直接
  実行するだけで clusterIP も MagicDNS も叩ける。kubectl write 不要 = `capabilities: []` と整合。
- **既存パターン**: `ops/tools/` の既存ツール (version_watch / dashboard_smoke / syncthing_acceptance)
  は標準ライブラリのみ (urllib / socket / argparse)。テストは `ops/tests/` に unittest を置き、
  `FakeFetcher` 相当で HTTP 層を差し替え network-free にする。CI は `python3 -m unittest discover
  -s ops/tests -t .` (.github/workflows/ci.yml)。**spec の verify は `python3 -m pytest` を
  要求しており、この sandbox には pytest が無い** — テストは pytest 実行でも通る書き方
  (unittest.TestCase ベース等) にするか、wrapper の verify 環境に合わせて調整を残す。
- **対象 Service**: アプリの入口は clusterIP Service (ops-dashboard 80 / coder 80+postgres /
  nats / vaultwarden / syncthing / immich postgres / autopilot heart-service) と tailnet 公開
  Service (adguard `LoadBalancer`+`loadBalancerClass: tailscale`, hostname `adguard` →
  MagicDNS `adguard.<tailnet>` の 53/tcp+udp と :3000; syncthing も同型)。adguard は
  clusterIP でも叩ける (LoadBalancer にも clusterIP が割り当たる)。
- **「DNS 死」の検知**: adguard の 53/tcp または 53/udp への応答がない・MagicDNS 名の解決
  が失敗することを probe が timeout/refused として検出する。fixture はこの応答なしを再現する。

### 決めてあること

- **1 本の `ops/tools/reachability_probe.py`**: 全 Service の clusterIP path + tailnet 公開アプリの
  MagicDNS path を叩き、各 path の ok/fail を `ops/health/reachability.json` に JSON で出力。
  `--selftest` は fixture (network-free) で判定ロジックを自己検証する。
- **`ops/tests/test_reachability_probe.py`** を `ops/tests/fixtures/` の fixture 付きで追加。
  adguard の「DNS 死」ケースを必ず含める (spec dod 2)。既存テスト群 (unittest discover) に乗る形。
- **baseline は docs に残す**: 今日の応答マップを `docs/` (例: `docs/reachability-baseline.md`)
  に書く。dod 3。実行した日時・環境・各 Service の実測結果を記録。
- **常設は CI 経由**: `touches_apps: false` のため apps/ に CronJob 等は足さない。計器の
  判定ロジックが壊れないことを fixture + unittest + `--selftest` で CI が守り、実機での
  実行はクラスタ内セッションからいつでも行える形にする (CI へのジョブ追加が妥当かは worker 判断)。

## やらないこと

- **画面・ダッシュボード・observability 系の変更**。spec は「k8s 本体の計器」で、P-0036 を
  枠の外 (observability) にしないため画面は作らない (VISION: 器を使い切る)。
- **apps/ 配下の manifest 変更** (`touches_apps: false`)。新しい CronJob・Service・Probe は足さない。
- **アラート配線 (Discord / ops-health-reporter への統合)**。検知結果をどう通知するかは
  本プロジェクトの外。ここでは計器・fixture・baseline の常設まで (spec dod の範囲)。
- **adguard の修繕・再発防止** (242 回 CrashLoopBackOff の根本対策)。本プロジェクトは
  「検知できるようにする」ことだけ。修繕は別の PR (1 PR 1 論点)。
- **ArgoCD health 判定の改修**。対象は Service 到達性であり、ArgoCD の health モデルは変えない。
- **外部公開 URL (Ingress 等) への到達性**。clusterIP / tailnet (MagicDNS) の dual-path のみ
  (spec タイトルの範囲)。
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)。
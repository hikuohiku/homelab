# P-9034 PROGRESS

## 2026-08-24（initializer）

- PROJECT.md を作成。対象 Service の manifest 実読、verify の実測 (3 項目とも failing)、
  設計方針を確定。worker は下の「2026-08-24 (worker 1)」へ。

## 2026-08-24（worker 1）

### やったこと (dod 1-3 を一括実装)

1. **`ops/tools/reachability_probe.py`** (dod 1): 標準ライブラリのみ。全 17 対象を
   clusterIP (Service DNS) / tailnet (MagicDNS) の dual-path で叩き、
   `ops/health/reachability.json` に出力。probe kind は http / tcp / udp / dns-tcp / dns-udp。
   adguard の DNS 死は dns-tcp/dns-udp (正当な DNS 応答を要求) が直接捕まえる。
   - 結果は **3 値 state**: `ok` / `fail` (名前解決は通ったが probe 失敗 = 死を確認) /
     `unknown` (名前解決失敗 = この実行コンテキストからは観測不能)。rc=1 は fail のときのみ。
   - resolver と probe はどちらも差し替え可能にし、テスト / `--selftest` は完全 network-free。
2. **`ops/tests/test_reachability_probe.py` + `ops/tests/fixtures/reachability/adguard-dns-dead.json`**
   (dod 2): adguard「DNS 死」を fixture で再現し、clusterIP / tailnet の 4 経路が fail に
   落ちることを unittest で固定。20 テスト green。pytest でも通る (unittest.TestCase ベース)。
3. **`docs/reachability-baseline.md`** (dod 3): 2026-08-24T23:34Z の実測マップを記録。
4. `.gitignore` に `ops/health/` を追加 (生成物のコミット防止。ops/dashboard/index.html と同型)。

### 分かったこと (実測。baseline doc に全文)

- **計器は 2026-08-24 当日の実インシデントを 1 回の実行で捕まえた**: adguard clusterIP
  53/3000 が refused/timeout (fail)、autopilot-heart:8099 が refused + dashboard の
  `heart.stale: true` (fail)。このプロジェクトの対象そのものが実行中だった。
- **initializer の前提の誤りを 1 つ発見**: 「in-cluster なら MagicDNS も叩ける」は
  **間違い**。runner Pod からは `adguard.*` / `syncthing-sync.*` が NXDOMAIN になる
  (CoreDNS → ts.net stub → ts-nameserver-fixed)。**健康な syncthing-sync の MagicDNS 名も
  NXDOMAIN** (clusterIP 22000/tcp は接続成功) なので、解決失敗だけでは死と断定できない →
  state=unknown を導入。tailnet 経路の本実測は tailnet メンバー (node01) から実行が必要。
- **autopilot-heart は NetworkPolicy が送信元を app=autopilot-core に限定** しており、
  runner 等の他 Pod からは正常時でも refused になりうる (k3s netpol は REJECT)。心臓の単独観測は
  実行コンテキストに依存する既知の死角。
- verify の `python3 -m pytest` はこの sandbox に pytest が無く実行不能だった
  (pip も無い)。unittest (同一収集経路) で green を確認。CI は `unittest discover` なので
  CI 上では pytest 無しで通る。wrapper の verify 環境に pytest があるならそのまま通るはず
  (unittest.TestCase ベースなので collect される)。
- F821 (ruff) はローカルに ruff (musl) を落として確認済み: `All checks passed!`。
  `ops/validate.py` / `check_doc_commands.py` も通過 (pre-existing warning のみ)。

### 次のセッションへの一言

- **残っているのは tailnet 経路の実測 (node01 等、tailnet メンバーから) だけ**。
  dod 1-3 のファイルは揃った。verify 3 項目のうち pytest はローカル未実測だが
  unittest で green。wrapper が verify を回して judge してほしい。
- baseline doc の「未解決 / 引き継ぎ」4 件のうち、特に (1) tailnet 実測の補完と
  (2) syncthing-sync の MagicDNS 名が tailnet 側でも NXDOMAIN なのか、を確認すると良い。
- adguard の死と autopilot-heart の stale は 2026-08-24T23:34Z 時点で継続中。
  修繕は本プロジェクト外 (1 PR 1 論点) — curriculum が拾う別プロジェクト。
- 自動実行 (CI ジョブ / CronJob) は `touches_apps: false` のため足していない。
  常設の形は「fixture + unittest + --selftest で判定ロジックを CI が守り、実機実行は
  セッションから」に留めた。自動化を足すならアラート配線 (外部) と合わせて設計が必要。
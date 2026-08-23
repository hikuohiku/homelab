# P-0203 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。

## 初期状態 (initializer, 2026-08-23)

- PROJECT.md を作成して commit。実装は未着手 (`ops/security/`, `docs/security/` 共に未存在)
- verify 4 項目とも failing を実測済み (詳細は PROJECT.md 受入チェックリスト)

## セッション 1 (worker, 2026-08-23) — 台帳スクリプト + 出力 + unittest を実装

### やったこと

1. `ops/security/egress_census.py` (stdlib のみ, pyyaml 不使用) を新設。
   `apps/**` (.yaml/.yml/.py/.go/.sh/.ts) + `nix/**` (.nix) + `ops/rules.json`
   を走査し、URL / `oci://` / restic `b2:` を抽出 → attribution 規則表
   (ATTRIBUTION_RULES) で workload/namespace/reason/breakage に変換 →
   provider 定数行 (Doppler / Tailscale coordination / rules.json allowlist 由来の
   Discord・Telegram・Anthropic / kubelet image pull) を追加して
   `docs/security/egress-census.{json,md}` を再生成する。
   `--check` は再生成差分ゼロを確認 (冪等)。**通信系 import はゼロ**
   (`socket`/`urllib` 等無し — grep 実測)、実クラスタへの通信試験はしていない
2. `ops/tests/test_egress_census.py` (26 tests) + fixture 4 種
   (`ops/tests/fixtures/egress_census/`)。抽出純関数を両方向固定
   (コメント内 URL を拾わない、`b2:$(VAR):` と literal bucket の両形、doc 境界で
   meta が混ざらない、attribution 不能な外部宛先で落ちる = fail-closed、
   必須 host 欠落・下限割れで落ちる)
3. 台帳の実績値: **38 endpoint / 12 namespace**。必須 4 host
   (api.doppler.com / api.backblazeb2.com / api.github.com / api.telegram.org) +
   accounts.google.com / controlplane.tailscale.com / github.com / discord.com /
   ghcr.io / docker.io を MANDATORY_HOSTS として fail-closed 固定

### verify 実測 (全 green)

- `python3 ops/security/egress_census.py --check` → rc=0 (38 endpoint 冪等)
- json schema 検査ワンライナー → rc=0
- md の grep (Doppler|Backblaze, 既定拒否) → rc=0
- `python3 -m unittest ops.tests.test_egress_census` → OK (26 tests)
- 追い: `python3 -m unittest discover -s ops/tests -t .` → OK (315 tests、既存壊し無し)

### verify が直接見ない DoD 項目の証跡

- 「既定拒否時に開けるべき穴」フラグ = JSON 各レコードの `open_at_default_deny`
  (true 33 行 / false 5 行。false は node レベルとリンク文字列生成のみの行)。
  autopilot 対象外にする場合の例外理由文言 = `exception_note` (autopilot ns の
  全行に trifecta seeds #11 由来の文言入り、MD にも節として出力)
- md 版は namespace 別表 +「この穴が塞がれると壊れるもの」列 +「横串」節 +
  除外ホスト節 + 盲点節で構成
- 実測プローブは未実施 (spec 明示どおり)。台帳の blind_spots に Syncthing discovery/
  Vaultwarden icon/LLM telemetry/DERP/kubelet pull を明記した

### 分かったこと / 設計判断

- **manifest に host 直書きが無い穴は「provider 定数」として明示的に載せた**
  (api.doppler.com, controlplane.tailscale.com, api.backblazeb2.com, api.anthropic.com,
  discord.com)。黙って補うのでなく source_evidence に「直書きが無い」ことを書くのが
  正直な形。PROJECT.md の設計判断ポイントへの回答
- **coder の workspace-home backup だけ b2: が CronJob 本体ではなく同梱 ConfigMap
  (workspace-home-backup-script) 内の埋め込み pod spec にある**。doc 名から workload を
  取る規則に override 表を持たせて解決 (ConfigMap 名 → 消費側 CronJob 名)
- version-watcher は GitHub 以外に hub.docker.com と registry.npmjs.org も見ていた
  (inventory の種別による)。初手の grep 一覧には出てこなかった発見
- 除外カテゴリを 3 種用意: cluster_local (*.svc 等), self_public_url (*.ts.net),
  schema_reference ($schema)。ts.net は「ブラウザ側の接続先」だが coder だけは
  CODER_ACCESS_URL が agent 接続先も兼ねるため盲点に昇格して記載

### 発見 (仕様外。curriculum が拾うこと)

- `apps/autopilot-core/app/main_test.go:181` / `mcp_test.go:106` に
  `http://127.0.0.1:1` というポート 1 宛の記述がある (テスト用モックと思われるが
  ポート選択の意図不明)
- nix/images/proxmox-cloud/k3s-manifests.nix は apps/argocd/kustomization.yaml と
  argo-cd chart の repo/version を二重管理している (check_version_sync.py が監視済み、
  新規問題ではない)

### 次のセッションへ

- レビュー差戻しがあればその解消が最優先。差戻しが無ければ **verify は全部通って
  いる状態で commit 済み**。wrapper の再実測で green になるはず
- 罠: attribution 規則は glob と host の組で引いている。**新しい URL を apps/ に足すと
  `--check` が「attribution できない」と落ちるのは仕様** (fail-closed)。
  エラーメッセージの指示どおり ATTRIBUTION_RULES に足すこと
- 罠: fixture から実リポジトリ走査を分離するため build_records/rules/provider_rows は
  注入可能にしてある。実 pipeline は `build_all()` だけが validate_census を呼ぶ
- 台帳の行数は現状 38。行の増減そのものより「workload が消えたのに台帳に残る」
  方向の方が危ないので、build_records は走査ベース (provider 定数以外は
  証跡行がある行しか載らない) にしてある。この性質は壊さないこと


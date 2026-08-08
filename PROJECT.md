# P-0004 — エージェント自身の脳と土台イメージのバージョンを固定し、監視対象に入れる

## 目的

`images/autopilot/Dockerfile` は `npm install -g @anthropic-ai/claude-code` を pin 無しで実行し、
`FROM` も `node:22-alpine` のタグ参照。イメージを再ビルドするたびに、このエージェントの思考エンジンと
OS が誰の判断も経ずに入れ替わる。`ops/inventory.json` は他人（ArgoCD・immich・vaultwarden…）の
バージョンを 30 件以上監視しているのに、自分自身だけが監視外という非対称がある。
#49（vaultwarden 1.36.0 の据え置き）の裏返しで、こちらは「誰も止めなければ勝手に上がる」。
壊れたときに「いつから壊れたか」を後から特定できない点が最も高くつく。

## 受入チェックリスト

wrapper が実測した結果、**3 項目とも現時点で failing**（2026-08-08、initializer が
`images/autopilot/Dockerfile` と `ops/inventory.json` に対して実行、いずれも exit=1）。

- [ ] `grep -qE '@anthropic-ai/claude-code@[0-9]+\.[0-9]+\.[0-9]+' images/autopilot/Dockerfile`
  — claude-code CLI が `x.y.z` の明示バージョン付きで install されていること（現状は pin 無し）
- [ ] `grep -qE '^FROM node:22-alpine@sha256:[0-9a-f]{64}' images/autopilot/Dockerfile`
  — ベースイメージが行頭 `FROM node:22-alpine@sha256:<64桁>` の digest 参照であること
    （タグ `node:22-alpine` は残したまま digest を付ける形。タグを消すと不一致になる）
- [ ] `python3 -c "import json,sys; ids=[t['id'] for t in json.load(open('ops/inventory.json'))['targets']]; sys.exit(0 if 'claude-code-cli' in ids else 1)"`
  — `ops/inventory.json` の `targets` に id `claude-code-cli` のエントリが存在すること

## 設計方針

- **実測はこのセッション自身が持っている。** worker は runner Job の Pod で動き、その Pod は
  `apps/autopilot/heart-deployment.yaml` の `RUNNER_IMAGE`（= Deployment と同じ
  `ghcr.io/hikuohiku/homelab-autopilot@sha256:6d0cf8b13d602f189d75b48ef654cb0cfd0fe999740782928199097b7451bf75`）
  から起動している。つまり `claude --version` / `node --version` / `/etc/os-release` を
  自分の中で叩いた結果が、そのまま「現在稼働中の Pod の実測」になる。initializer の実測値:
  **claude-code 2.1.223 / node v22.23.2 / Alpine 3.24.1**（`npm ls -g --depth=0` でも
  `@anthropic-ai/claude-code@2.1.223` を確認）。worker は自分でも叩き直して PR 本文に残すこと。
- **pin する値は「今動いているもの」に合わせる。** claude-code は `2.1.223`。ここで最新版に
  上げるのは別の論点であり、この PR は挙動を変えない（VISION「ループが止まらないこと」）。
- **node のベース digest は Docker Hub の registry API で取れる。** in-cluster からは到達する
  （initializer が実測）。`auth.docker.io` でトークンを取り、`registry-1.docker.io` に
  `HEAD /v2/library/node/manifests/22-alpine` して `docker-content-digest` を読む。
  2026-08-08 時点の index digest は `sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32`
  で、その amd64 manifest の config を辿ると `NODE_VERSION=22.23.2` — 稼働中の Pod と一致するので
  **この digest を指す限り中身は変わらない**（= 挙動を変えない pin になる）。値は worker が
  自分で取り直して確認すること。**必ず index（マルチアーキ）の digest を使う**。
  単一アーキの manifest digest を書くと GitHub Actions の runner アーキが変わったときに壊れる。
- **`ops/inventory.json` に 2 エントリ追加する。** `check_inventory`（`ops/validate.py`）が
  `id` / `kind` / `name` / `current` / `file` / `upstream` / `policy` の非空と `file` の実在、
  `policy ∈ {auto, manual, pinned}` を検査する。
  - `claude-code-cli` — 自分の思考エンジン。`policy: manual` を推奨する（壊れると全セッションが
    起動せずループが止まる = 自力で直せない。他の「到達性」系エントリと同じ扱い）。
    `observability_impact` に「壊れると全セッションが起動しない = ループが止まる」を書く
  - node ベースイメージ（id は `autopilot-base-image` 等）— `kind: image`,
    `upstream: dockerhub:library/node`。こちらも `policy: manual`
- **Dockerfile 冒頭コメントに digest 更新手順を書く。** `.github/workflows/build-autopilot-image.yml`
  は `main` への push で ghcr.io に push するだけで、manifest 側の digest は手動更新の運用。
  この PR が merge されてイメージが再ビルドされた**後**に、Actions のログ（`::notice title=autopilot image::`）
  の digest を manifest に反映する**別 PR** が要る。反映先は 3 箇所:
  `apps/autopilot/deployment.yaml`（image）、`apps/autopilot/heart-deployment.yaml`（image と
  `RUNNER_IMAGE` env）。`ops/check_version_sync.py` の GROUPS がこの一致を検査する。
- **CI は落ちない。** `ops/check_autopilot_image_pin.py`（`images/autopilot/` を変えたのに
  deployment の digest が変わっていなければ落とすスクリプト）は存在するが
  `.github/workflows/` のどこからも呼ばれていない（配線は未採択の P-0003 の題目）。
  したがって「Dockerfile だけ変えて digest は次の PR」という順序で CI に阻まれることはない。

## やらないこと

- **claude-code / node のバージョンを上げること。** この PR は現状を固定するだけ。更新は
  inventory に載った後、通常のバージョン更新の作法（CHARTER §4）で別途扱う
- **`apps/autopilot/deployment.yaml` / `heart-deployment.yaml` の image digest 更新。**
  新しい digest はこの PR が merge されてイメージが再ビルドされるまで存在しない。手順を
  Dockerfile 冒頭コメントに残すところまでが本プロジェクトの範囲（spec の DoD 3 に明記）
- **`ops/check_autopilot_image_pin.py` を CI に配線すること。** 未採択の P-0003 の題目であり、
  別の論点（1 PR 1 論点）
- **`ops/memory/substrate.md` の「claude-code (npm pin 無し)」記述の更新。** `ops/memory/` の
  書き手は consolidation の PR のみ（`ops/memory/README.md`）。ここでは触らない
- **Dockerfile に入っている他の pin（`KUBECTL_VERSION`、apk パッケージ）の見直し。**
  inventory への追加も含めて別論点

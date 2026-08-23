# P-0203 — NetworkPolicy 案が 4 回死んでいるのは「開けるべき穴の実測一覧」が一度も作られていないからだ — クラスタ全体の外向き通信 (egress) の全数台帳を作る

## 目的

既定拒否 NetworkPolicy の導入は P-0039 / P-0086 / P-0129 / P-0178 と 4 回立案され 4 回進んでいない
(ops/projects/archive.jsonl 実測。いずれも delivered になっていない)。共通の未解決前提は
「どの workload がどの外部宛先に本当に届く必要があるか」の一覧が存在しないことで、推測のまま
既定拒否を敷けば backup (B2)、External Secrets (Doppler)、器自身 (GitHub/Telegram) を断って事故る。
だから実装の前に、**manifest から機械的に再生成できる外向き通信の全数台帳**を先に作る。
内向きの全数調査は P-0095 (認証なし応答面) で型があるが、外向きは過去 201 案で一度も扱われていない。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0203` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 ops/security/egress_census.py --check`
  — 台帳生成スクリプトが存在し、再実行しても差分ゼロ (冪等) であること。
  実測 rc=2 (`ops/security/` 自体が未存在、スクリプト無し)。
- [ ] `python3 -c "import json; d=json.load(open('docs/security/egress-census.json')); eps=d.get('endpoints',[]); hosts={e['endpoint'].split(':')[0] for e in eps}; need={'api.doppler.com','api.backblazeb2.com','api.github.com','api.telegram.org'}; assert len(eps)>=8 and need <= hosts and all(e.get('reason') and e.get('workload') for e in eps)"`
  — 機械可読台帳が schema 通り存在し、8 endpoint 以上・必須 4 ホストを実名で載せ・
  全レコードに workload と reason が入っていること。
  実測 rc=1 (`FileNotFoundError: docs/security/egress-census.json`)。
- [ ] `test -s docs/security/egress-census.md && grep -qE 'Doppler|Backblaze' docs/security/egress-census.md && grep -qE '既定拒否|default-deny' docs/security/egress-census.md`
  — 人間可読台帳が空でなく、主要依存と「既定拒否時に壊れる」注記を含むこと。
  実測 rc=1 (`docs/security/egress-census.md` 未存在)。
- [ ] `python3 -m unittest ops.tests.test_egress_census`
  — YAML からの endpoint 抽出ロジックが fixture ベースの unittest で固定されていること。
  実測 rc=1 (ModuleNotFoundError — テストモジュール未存在)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) 各 endpoint の「既定拒否時に開けるべき穴」フラグと、autopilot namespace を対象外にする
場合の例外理由文言が実在すること、(2) md 版の namespace 別表と「この穴が塞がれると壊れるもの」
注記が実質的に読めること、(3) **実クラスタへの通信試験を一切しない**こと (静的台帳のみ) —
は worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- spec が想定する材料は全部 manifest 側にある:
  - **Doppler**: ClusterSecretStore `doppler` (`apps/external-secrets/cluster-secret-store.yaml`)
    経由で ESO が `api.doppler.com:443` に取りに行く。ExternalSecret は immich / vaultwarden /
    coder / dex / autopilot / syncthing / telegram-adapter 等 12 namespace に散在
    (P-0175 PROJECT.md の実数)。穴が塞がると全 Secret 同期が止まる
  - **B2**: restic の `RESTIC_REPOSITORY` が `b2:$(RESTIC_B2_BUCKET):…` 形で 5 リポジトリ
    (immich / vaultwarden / coder-postgres / coder-workspace-homes / syncthing、
    各 `apps/*/restic-backup-cronjob.yaml`) → `api.backblazeb2.com`。credential も Doppler 由来
  - **GitHub**: version-watcher (`version_watch.py` / `watch.py`)、ops-health-reporter
    (`report.py`)、telegram-adapter (`app/main.go` の `GITHUB_API` 既定値)、autopilot 本体
    (`ops/heart/gh.py` + git push) → `api.github.com` / `github.com`
  - **Telegram**: telegram-adapter `TELEGRAM_API` 既定値 → `api.telegram.org`;
    autopilot も `TELEGRAM_BOT_TOKEN` を持つ (`ops/rules.json` allowlist)
  - **Google OIDC 上流**: Dex connector の issuer `https://accounts.google.com`
    (`apps/dex/values.yaml`) → discovery/token 取得
  - **Tailscale coordination**: tailscale-operator (Helm `pkgs.tailscale.com/helmcharts`) の
    proxy Pod 群が coordination server と話す。repo 内に host 名は直書きされていないので
    source_evidence は operator の構成ファイルになる — 「manifest から名前解決できない穴」を
    どう記載するかが worker の設計判断ポイント
  - **コンテナレジストリ**: `ghcr.io` (自前イメージ群) / `docker.io` (valkey,
    version-watcher の `python:3.14-alpine`)。ただし **image pull は kubelet = ノード側 egress で
    Pod NetworkPolicy の対象外**という重要な注意付きで載せる。一方 argocd-repo-server の
    Helm chart fetch (`charts.dexidp.io`, `argoproj.github.io/argo-helm`,
    `charts.external-secrets.io`, `oci://ghcr.io/immich-app/immich-charts` 等、各
    `apps/*/kustomization.yaml`) は Pod レベルの通信なので対象内
  - **器の通知**: Discord webhook (`discord.com/api/webhooks`, `ops/heart/notify.py`) は
    autopilot namespace から。LLM API endpoint (OPENCODE_API_KEY の接続先) は repo に直書きが無い
    — 載らない理由を台帳側に明記するのが正直な扱い
- スクリプトの流儀: `ops/check_*.py` 群と同じ stdlib のみ (`ops/rules.json` _comment:
  「CI とサンドボックスを stdlib のみで通す repo 慣習」。イメージには pyyaml があるが spec が
  stdlib を明示)。YAML は簡易パース (行ベース/正規表現) で endpoint を抜き、判定ロジックは
  純関数に分けて合成 fixture を両方向固定 — check スクリプトの定石 (P-0071/P-0105/P-0185 と同型)
- unittest は `ops/tests/test_*.py` + fixture は `ops/tests/fixtures/<主題>/`
  (既存例: `ops/tests/fixtures/engine_stderr/`)
- 出力先 `docs/security/` は未存在だが、docs/ 配下に人間可読文書を置く流儀は確立済み
  (`docs/backup.md`, `docs/doppler-outage-runbook.md` 等)

### 作り方

1. `ops/security/egress_census.py` — `apps/**` の YAML (ExternalSecret, CronJob env,
   image:, kustomization の helm repo, Application repoURL)、`.py` / `.go` の直書き URL、
   `ops/rules.json` の `allowed_autopilot_doppler_keys` (→ DISCORD_WEBHOOK_URL /
   TELEGRAM_BOT_TOKEN 等の帰結先) を走査し、
   `{workload, namespace, endpoint(host[:port]), reason, source_evidence}` に加えて
   **「既定拒否時に開けるべき穴」フラグ + autopilot namespace 対象外の場合の例外理由文言**
   を付ける。endpoint は重約束 8 種 (Doppler / B2 / GitHub / Telegram / Google OIDC /
   tailscale coordination / レジストリ系 / Discord) を最低ラインとして実名で載せる
2. 出力は `docs/security/egress-census.json` (機械可読) と `docs/security/egress-census.md`
   (人間可読: namespace 別表 + 「この穴が塞がれると壊れるもの」の注記)。
   `--check` で再生成差分ゼロを確認
3. `ops/tests/test_egress_census.py` — 抽出ロジックを fixture で unittest (正常系・
   抽出漏れになりうる形・壊れ入力で落ちること)。実クラスタへの通信は一切しない
   (静的台帳のみ。実測プローブは次のプロジェクト)

## やらないこと

- **NetworkPolicy の適用・`apps/` 配下の変更** (spec `touches_apps: false`)。
  既定拒否を敷くのは台帳の後続プロジェクト (P-0039/P-0086/P-0129/P-0178 の血統) であり、
  本案は台帳を作るまで
- **実クラスタへの通信試験・プローブ** (spec DoD (5) の明示)。「届く必要がある」は manifest と
  コードからの静的推論であり、実際の到達性検証は次のプロジェクト
- **autopilot namespace の例外設計の決定**。例外理由文言を台帳に添えるだけで、
  方針 (trifecta 分離 seeds #11 との整合) は別論点
- **新規 credential の発行・SOPS/Doppler の変更**。読むだけ
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする
  (CLAUDE.md)

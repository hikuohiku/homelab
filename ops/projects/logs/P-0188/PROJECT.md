# P-0188 — 証明書の死は当日まで誰にも見えない — クラスタ内 TLS Secret と Proxmox pveproxy 証明書の残り日数を台帳にし、30 日前から鳴らす

## 目的

「現行証明書がいつ切れるか」を機械で見ている者がいない。pveproxy 証明書が切れれば
Proxmox への到達自体が死んで復旧手順の前提が崩れ、クラスタ内 (ArgoCD/Dex 等の手作り
TLS Secret) も失効まで無音。P-0105 (SOPS 地図)・P-0144 (tailnet 鍵期限) と同じ
『秘密の賞味期限』系列で、証明書だけがまだ台帳化されていない。health レポートへの
畳み込みは既存様式があり安価。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0188` の checkout、リポジトリルートから実行)。

- [ ] `test -f ops/tools/check_cert_expiry.py`
  — 期限台帳スクリプトが存在すること。実測 rc=1 (ファイル不在)。
- [ ] `python3 -m unittest ops.tests.test_cert_expiry`
  — fixture ベースの固定テスト (正常・期限切れ・パース不能の 3 系列) が存在し green
    であること。実測 FAILED (errors=1、モジュール不在による import error)。
- [ ] `python3 ops/tools/check_cert_expiry.py --fixture ops/tests/fixtures/cert_expiry/sample.json > /dev/null`
  — スクリプトがクラスタ/Proxmox 非到達でも fixture を読んで最後まで動くこと
    (ロジックとI/O の分離)。実測 rc=2 (スクリプト不在)。
- [ ] `grep -q 'cert' apps/ops-health-reporter/report.py`
  — health レポートへ証明書セクションが配線されていること。実測 rc=1
    ('cert' を含む行なし)。

**verify は DoD の下限であって DoD そのものではない。** grep/unittest の green は
「30 日前から鳴る」「SAN 不一致の解消判定が正しい」を見ていない。しきい値判定と
T-0107 フィールドの中身は PROGRESS.md に証跡を残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測した。調べ直さなくてよい)

- **レポートへの畳み込みの様式**は `apps/ops-health-reporter/report.py` の
  `collect(collect_*)` → `latest.json` + `history/YYYY-MM-DD.jsonl`。前例:
  `download_budget` (P-0128, 純関数モジュール `download_budget.py` を import)、
  `externalsecrets` (P-0175)。判定は status 文字列に落とすのが流儀
  (download_budget の cap 判定 ok/warn/exceed/unconfigured/no_data 同型)。
  新しい純関数モジュールを /scripts に載せるには
  `apps/ops-health-reporter/kustomization.yaml` の configMapGenerator への追記が要る。
  verify #4 は report.py 本体を grep するので、配線箇所は report.py 側に見えること。
- **heart 側の鳴らし方の前例**: `ops/heart/heart.py` 401-442 行目。レポートの
  status を見て `briefing-queue.jsonl` への積み込み + incident 通知の 2 流路に乗せ、
  cursors で同一日内の再通知を落としている (budget_alert_due)。30 日未満 = briefing /
  7 日未満 = incident のしきい値判定もこの位置に同型で足すのが自然。
- **RBAC の隙間**: reporter の ClusterRole `ops-health-reporter-reader`
  (`apps/ops-health-reporter/rbac.yaml`) は secrets を一切読めない。TLS Secret の
  列挙には `secrets` の get/list 追加が必須。**値 (tls.key 等) は絶対に出力せず、
  tls.crt を DER パースして notAfter/SAN を抽出した結果だけを載せる**旨を rbac.yaml
  のコメントに既存エントリ同様書き添えること。クラスタ内 API アクセスは kubectl
  サブプロセスではなく SA token + urllib が reporter の既存経路 (spec の「kubectl で」
  は手段の指定ではなく列挙対象の指定と解釈してよい)。
- **Proxmox 側**: `GET /nodes/{node}/certificates/info` は Sys.Audit 権限で、agent
  token (PVEAuditor) の読み取り権限内。外側からの検証は既に
  `ops/tools/check_pve_tls.sh` (P-0103, python3 ssl のみ) があるが、これは到達時点の
  検証であり期限の台帳とは層が違う (併存)。T-0107 の実測事実: 現行 pveproxy 証明書の
  SAN は 127.0.0.1/::1/192.168.1.2... で、接続先
  `hikuo-homeserver.tailae6c2.ts.net` と不一致。真因は SAN 不一致単体ではなく
  「CA を誰も信頼していない」ことで、解消候補は `tailscale cert`
  (`docs/pveproxy-tls.md`)。**解消自動判定は「期待する接続先名が SAN に含まれるか」
  の機械比較としてフィールド化すればよい** (信頼の問題まで見に行かなくてよいのは
  check_pve_tls.sh の担当)。
- **stdlib のみ**。report.py の冒頭宣言どおり pip install を要求しない。openssl CLI も
  イメージに無いため (check_pve_tls.sh が python3 ssl で書かれた理由)、PEM→DER の
  base64 デコードと ASN.1 の最小ウォーク (notAfter の UTCTime/GeneralizedTime、
  SAN extension) は自前実装になる。spec の「DER パースは最小限の自前実装」はこの制約。
- **テストの流儀**: `python3 -m unittest ops.tests.test_cert_expiry` が verify に
  名指しされているので `ops/tests/test_cert_expiry.py` + unittest (pytest はイメージに
  無い)。fixture は `ops/tests/fixtures/cert_expiry/` 配下 (既存: engine_stderr/,
  syncthing-fixture-cert.pem)。verify #3 のとおり fixture は JSON で渡し、
  スクリプトは `--fixture` でオフライン動作できるようにする。

### 方針

1. `ops/tools/check_cert_expiry.py` を収集ロジック本体として作る。(a) k8s TLS Secret
   列挙・(b) Proxmox certificates/info 取得・(c) T-0107 判定フィールドを持ち、
   `--fixture` で記録済み応答を読んでオフラインで走る。出力は JSON (タイムスタンプは
   実行時刻として持たせず、証明書由来の日付だけにする — sops_dependency_map の
   「変化のない実行で diff を汚さない」流儀)。
2. 残り日数から status を 3 値に落とす (ok / warn <30d / expired 相当 <7d で
   incident 級)。report.py に collect_cert_expiry() としてセクション追加し、heart 側は
   budget_alert_due 同型で warn 以上を briefing、incident 級を通知へ。
3. テストは 3 系列 (正常・期限切れ・パース不能) を fixture で固定。パース不能な
   Secret は「無視」ではなく台帳に `parse_error` として載せて fail-closed に寄せる
   (sops_dependency_map の「何も見つけられないのは整合ではなく失敗」と同じ思想)。

## やらないこと

- **証明書の更新・再発行・差し替え本体**。T-0107 の解消作業 (`tailscale cert` 等) は
  人間/別タスクの論点。本プロジェクトは観測と鳴らしのみ
- **terraform apply 禁止の解除判断・解除実行**。SAN フィールド化で「解除条件が満たされた
  か」を機械判定できるようにするだけで、禁止自体は触らない
- **cert-manager の導入・非管理証明書の cert-manager 移行**。現状の証明書群をあるがまま
  台帳に載せるのが本プロジェクト
- **Secret の値・証明書本体の出力**。notAfter/SAN/残日数など判定に必要な派生値のみ
- **CI 配線の追加**。spec の verify に CI 項目が無い (unittest は CI の既存 ops job が
  discover するはずだが、新 job の追加は別論点)
- **memory limits の新設** — substrate の規則 (実測の裏付けなしに付けない) を継続
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。heart が直接 push
  する領域でコンフリクトする (CLAUDE.md)

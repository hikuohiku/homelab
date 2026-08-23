# P-0196 — application-controller は今日も OOMKill される — 「512Mi が足りない」のか「リーク」なのかを隣室の ArgoCD で負荷をかけ分けて切り分け、メジャー更新判断の材料を同時に産む

## 目的

health latest.json で argocd-application-controller が exit 137 OOMKilled (restarts 4,
2026-08-23T08:57:03Z 実見)。P-0181 (採択済み・stalled) は本番のメモリ系列を取るだけで
「なぜ」に答えられず、「上げて様子見」で終わってしまう。そこで隔離環境に**現行チャート
(9.1.6) と最新チャートの ArgoCD を並べて**同量の合成 Application を食わせ、application-controller
の RSS 曲線を比較する。(a) 定常所要メモリ (b) 負荷に対する増加率 (c) 新版での改善有無を一度に
測れれば、「512Mi が足りない」のか「リーク」なのかが分かると同時に、policy=manual で 20 日以上
凍結中の argocd-chart の更新を人間に勧められる初めての実測根拠になる。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-23、`project/p-0196` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 ops/projects/scripts/argocd_oom_lab.py --plan`
  — lab の計画 (作成するオブジェクト・負荷量・サンプリング計画) を出すスクリプトが存在し、
  `--plan` がクラスタへ触れずに通ること。
  実測 rc=2 (`can't open file '.../argocd_oom_lab.py': No such file or directory`
  — `ops/projects/scripts/` 自体が未存在)。
- [ ] `python3 -c "import json;d=json.load(open('ops/projects/logs/argocd-oom-lab/verdict.json'));assert d.get('conclusion') in ('insufficient-request','leak','chart-regression') and d.get('rss_series_csv')"`
  — 測定の結論が 3 分類のいずれかとして宣言され、根拠となる RSS 系列の CSV への参照を持つこと。
  実測 rc=1 (FileNotFoundError — `ops/projects/logs/argocd-oom-lab/verdict.json` 未存在)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) **lab が実際に kubectl-write で構築された**こと (紙上計画でない。--plan は通っても
実測が無ければ verdict は出せない仕組み上、verify 通過＝実施済みにはなるが、経緯は
PROGRESS.md へ)、(2) 両系統が**同一負荷・同一時刻刻窓**で測られていること (比較実験の要諦)、
(3) **検証後の lab 削除と残置物ゼロ** (spec dod の明示。verify は見ない)、(4) conclusion が
曲線の実測形状から導かれたものであり、望む結論からの逆算でないこと — は worker が
verdict.json の補助フィールドと PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- **本番証拠**: `origin/ops-health-report:ops/health/latest.json` (generated_at
  2026-08-23T12:30:09Z) の pod_issues[0] が `argocd/application-controller-0`
  exit 137 OOMKilled、restarts 4。
- **現行 pin**: `apps/argocd/kustomization.yaml:9` が chart `argo-cd` 9.1.6
  (appVersion v3.2.1 — index.yaml 実取得で確認)。controller の resources は
  `apps/argocd/values.yaml:56-63` (limits 512Mi / requests 256Mi、出典のない推測値)。
  `ops/inventory.json` の `argocd-chart` は `policy: manual` で凍結中 — spec の前提どおり。
- **最新チャート**: argo-helm index.yaml 実取得で `argo-cd 10.4.0` (appVersion v3.5.1) が最新。
  chart tgz は `github.com/argoproj/argo-helm/releases/download/argo-cd-<ver>/...` から
  取得可能 (9.1.6 の tgz 204KB を実ダウンロードして確認)。
- **helm はイメージに無い** (substrate)。ただし `get.helm.sh` の静的バイナリが到達可能なことを
  実測 (HTTP 200, ~17MB)。作業領域に落とした `helm template` → `kubectl apply` で render するのが
  素直 (helm は image・repo に入れない使い捨て)。`raw.githubusercontent.com` も到達可。
- **node01 は 4 vCPU / 11.7 GiB allocatable、requests 現在約 1.2 CPU / 2.6 GiB** (substrate)。
  2 套追加は軽量化設定 (dex / notifications / applicationSet / server 等を無効化、redis は
  HA 無し) で収める。lab の memory limits は「**被験体を途中で殺さないための十分な上限**」を
  明示的に付けて値を記録する — T-0055 の教訓は「根拠なく小さく縛るな」であり、測定対象の
  OOMKill は計測の失敗である。CPU requests も控えめにしてノードを圧迫しない。
- **同一 namespace に 2 套入れるなら tracking の分離が必須**: 既定では application-controller は
  自身と同じ namespace の全 Application を処理するので、2 套が 30 本を取り合う。
  `application.instanceLabel` (configmap `argocd-cm`) を系統ごとに変えて互いのアプリを無視させるか、
  namespace を 2 つに分ける (spec の "argocd-lab" を接頭辞として解釈)。worker が実機で確認して選ぶ。
- **CRD はクラスタスコープで共有される**: `applications.argoproj.io` 等を両系統で二重適用すると
  immutable 項目で競合しうる。CRD は先に 1 回だけ適用し 2 套目はスキップ、が単純。
- **合成 Application の負荷源**: 到達可能な公開 example リポジトリ (github.com は到達性実測済み) の
  別 path × 30 本を、destination namespace = lab 側に向けるのを推奨。fetch → diff → status の
  一連の仕事を実際に走らせられる。到達不能 URL は fetch 失敗だけで負荷の質が変わってしまう。
  sync 対象は lab 内の小さい ConfigMap 等に限定し、本番リソースを決して指させない。
- **kubectl は `autopilot:autopilot-writer` SA で動く** (P-0175 実測: netpol 作削除 / pods 作成 /
  exec 可、secret・SA list 不可)。namespace 作成権限の有無は未実測 — 初手で確認し、
  無ければ代替案を PROGRESS.md に記す。
- **(セッション 2 追記) chart 由来の ArgoCD には自前 SA + Role + RoleBinding が必要だが、
  autopilot-writer は RBAC 作成を意図的に持たない** (`apps/autopilot/rbac.yaml`:
  「自分の権限を自分で広げる経路を作らない」。CRD の get も不可 — 存在確認は discovery 経由)。
  up は ESO 同期まで成功した後、この壁で中断。解消には人間の一手
  (`ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml` の適用) を要求中。
  詳細と棄却した代替案は PROGRESS.md セッション 2。
- **監視ループは親 shell に引きずられて死ぬことがある** (P-0175 実測) → サンプラは
  `setsid` + stdin リダイレクトで起動し、進捗は CSV への追約で残す。セッションを跨ぐ場合は
  git log + PROGRESS.md + commit済み CSV から再開できるよう、CSV は逐次 commit する。
  サンプリング本体はスクリプトのサブプロセス実行にすれば、待ち時間のトークン消費はほぼゼロ。
- **metrics-server が稼働中** (pod_issues に常時出現) → `kubectl top pod` が使える。
  取れるのは working set (RSS の近似) であることを verdict に明記する。
  加えて各 Pod の restart 数を同時刻で取る (lab 内で OOMKill が起きたらそれは結果の一部)。
- **スクリプトの流儀**: stdlib (+ py3-yaml) のみ。`collect(fn)` パターンや AST 抽出テストは
  既存チェッカー群 (`ops/check_*.py`, `apps/ops-health-reporter/report.py`) の定石。
  `--plan` 単体でクラスタ非接触を保つ (verify 第 1 項の意味)。

### 作り方

1. **`ops/projects/scripts/argocd_oom_lab.py`**: サブコマンド形式 (例: `--plan` / 構築 /
   サンプル採取 / 判定)。`--plan` はノードの空き容量確認・作成予定オブジェクト一覧・
   サンプリング計画を出して終わる
2. **lab 構築 + 負荷投入**: helm template で軽量化 values を載せた現行 9.1.6 / 最新 10.4.0 の
   manifest を render → argocd-lab 系 namespace へ適用 → 各 30 本の合成 Application を投入。
   本番 `argocd` namespace とは完全に隔離する
3. **サンプリング**: 15 分間隔 × 数時間 (複数回の refresh/reconcile 周期をカバーする窓)、
   両系統を同時刻で `kubectl top pod` + restart 数を CSV 追記。CSV は逐次 commit
4. **判定**: 曲線形状で 3 分類。目安 — **insufficient-request**: 両系統とも平坦で、所要が
   512Mi 相当を超える/超えうる (リーク無しの単純不足)。**leak**: 右肩上がりが続いて平台に
   ならない (新版でも同じなら chart 非依存のリーク)。**chart-regression**: 同一負荷で新旧の
   曲線が有意に乖離 (どちら向きの乖離かも記録)。verdict.json は `conclusion` +
   `rss_series_csv` (必須キー) に加え、測定窓・負荷量・limits・補足を自由に載せてよい
5. **lab 削除**: 全オブジェクト (namespace 単位) を削除し、残置ゼロを確認して PROGRESS.md に記す

## やらないこと

- **`apps/argocd/values.yaml` の変更・chart pin の更新** (spec `touches_apps: false`)。
  本番の resources 引き直しは P-0181 の領域であり、本案はその判断材料 (実測) を産むだけ。
  「測ったから上げる」の逆算もしない
- **本番 `argocd` namespace への一切の変更** (本番 controller の再起動・設定変更を含む)。
  対象は lab のみ。ArgoCD 自身が壊れるとデプロイ経路ごと失う (inventory.json
  observability_impact) ので、本番側は見るだけ
- **Git → ArgoCD 経路外への恒久オブジェクト作成**。lab は CHARTER §5 の例外として spec
  capabilities (kubectl-write) が明示した一時物であり、検証後に必ず削除する。
  常設の隔離環境を作ることはしない
- **P-0181 自体の遂行**。stalled の別プロジェクトであり触らない (本件の成果は seeds 等を
  通じて間接的に役に立つ)
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)

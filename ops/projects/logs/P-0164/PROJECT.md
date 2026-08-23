# P-0164 — 癒し手が死んでいる間、デプロイ経路はどうなるか — ArgoCD を意図的に止めて Git 先行状態の傷を実測し、復帰時の追いつき時間を秒で出す

## 目的

このリポジトリ唯一のデプロイ経路 Git → CI → ArgoCD について、**ArgoCD 自身が止まっている間に
main が進んだら何が起きるか**を測った者はいない。既存の復旧計測系 (P-0080 RTO 復元演習 /
P-0094 canary 自壊案) はすべて「ArgoCD が生きていてアプリが死ぬ」方向で、「癒し手自身が死ぬ」
方向は未開拓。inventory argocd-chart の observability_impact が警告する「revert を適用する手段
ごと失われる」状態を平時に演じ、停止秒数・追いつき秒数・取りこぼしの有無を数字に残す。
次に ArgoCD 更新 (policy=manual の据え置きが続く) に臨むとき、怖いものを名前で言えるようにする。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0164` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/projects/logs/P-0164/report.json && python3 -c "import json;d=json.load(open('ops/projects/logs/P-0164/report.json'));assert 'catchup_seconds' in d"`
  — 演習レポートが存在し、scale 1 から全変更がクラスタに反映され切るまでの壁時計
  `catchup_seconds` を持つこと。実測 rc=1 (`ops/projects/logs/P-0164/` ごと未存在)。
- [ ] `python3 -m unittest ops.tests.test_deploy_continuity`
  — レポート集計ロジック (実測値の検算・集約) の unittest が存在し、クラスタなしで通ること。
  実測 FAILED (errors=1, モジュール未存在)。
- [ ] `python3 ops/tools/deploy_continuity.py --dry-run`
  — 演習統括スクリプトが存在し、dry-run モードがクラスタへの書き込みなしに完走すること
  (安全弁の判定結果などが見えること)。実測 rc=2 (スクリプト未存在)。

verify は DoD の下限であって DoD そのものではない。DoD (2)(3) の「同期順序・heart/health 側の
見え方・watcher/critic の報告」は verify が見張らない — PROGRESS.md に証跡 (コマンド出力の
引用と時刻) を残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- ArgoCD は apps/argocd を argo-cd Helm chart **9.1.6** (releaseName: `argocd`, namespace:
  `argocd`) で導入 (apps/argocd/kustomization.yaml)。fullnameOverride は無く、release 名と
  chart 名の縮退一致でリソース名が `argocd-*` になるため、spec 表記どおりの Deployment 名は
  `argocd-server` / `argocd-repo-server` / `argocd-application-controller`。
- values.yaml は replicas を明示していない (server/controller/repoServer とも resources のみ)
  → Git 上の希望状態は各 1 replica。argocd Application は `selfHeal: true`
  (apps/argocd/application.yaml:23) なので、scale 0 への kubectl 直変更は controller 復帰後に
  self-heal で戻りうる。演習は自分で scale 1 するので矛盾しないが、「自力でどこまで戻るか」も
  記録対象になる (取りこぼし観測と同じ系列)。
- **main への直 push は ruleset が拒否する** (substrate.md 実測)。「main に 2 commit」は
  merge_method=merge の PR 経由で積む (commit は保たれる)。ArgoCD 停止中でも GitHub API による
  merge は影響を受けない — これが「revert 手段ごと失われる」状態の本体である。
- 可逆な小変更の当て先: **子 Application の metadata.labels / annotations**
  (apps/&lt;app&gt;/application.yaml 等)。Application CRD の metadata 変更はワークロードを
  再起動させず (touches_apps=false と両立)、sync 後に `kubectl get application -n argocd &lt;name&gt;
  -o jsonpath` でラベルと `status.sync.revision` が新 main を指すことで「追いついた」を機械判定
  できる。docs-only commit は ArgoCD の管理外なので同期が発生せず計測にならない。2 commit は
  別アプリに当てると同期順序 (controller がどちらから処理するか) も観測できる。
- 安全弁のデータ源: **projects.json は main ではなく `ops-state` ブランチにある**
  (`git show origin/ops-state:projects.json`。ops/heart/adoptgate.py の clone_fresh() /
  明示 refspec 参照。shallow clone 単独 fetch の refspec 罠 — substrate.md git 節)。
  announced/active が 1 件でもあるなら演習は開始しない (他プロジェクトのデプロイを凍結させる
  ことになるため)。--dry-run はこの判定だけを行って終わる形が望ましい。
- 演習中も heart は autopilot namespace で生き続ける (ArgoCD に依存しない)。health reporter
  は CronJob で `kubectl get applications -n argocd` 相当を読むが、controller 停止中は
  status の鮮度が落ちるはず — この「見え方の変化」が DoD (2)(3) の観測対象。watcher は
  ops/tools/version_watch.py、critic の報告は result.json / review 系の記録で確認する。
- unittest の既存パターンは ops/tests/test_*.py (純関数+fixture 方式。例:
  test_backup_coverage.py, test_version_watch.py)。集計ロジックは report.json の辞書を入力に
  取る純関数にして、クラスタなしで試験可能にする。

### 作り方

1. `ops/tools/deploy_continuity.py` に安全弁 (ops-state の projects.json に announced/active
   が 0 件であることの確認) → 演習手順の統括 → 実測値の report.json 書き出しを持たせる。
   集計・検算部分は関数として切り出し、unittest から呼べるようにする。
2. 演習本体は spec の順で固定: scale 0 (3 Deployment 一斉) → 停止確認 → main へ可逆小変更
   2 commit (PR 2 本または 1 PR 2 commit。merge で main に積む) → 所定時間待機 → scale 1 →
   `status.sync.revision` が両 commit を指すまでの壁時計を秒で計測。同期順序・self-heal の
   動き・heart/health/watcher の見え方を時刻付きで記録する。
3. report.json は停止秒数・追いつき秒数 (`catchup_seconds` 必須)・取りこぼしの有無・
   watcher/critic が何を報じたかを持つ。演習全体は短時間 (30 分以内の目安) で完結させ、
   終了時に必ず scale 1 へ復帰させて終わる (異常終了時も scale 1 が最後の状態になるよう
   スクリプト側で担保)。
4. kubectl-write は **ArgoCD 一式 3 Deployment の scale 操作のみ**に使用 (spec の制約)。
   dex / redis / applicationset-controller には触れない。それ以外の変更はすべて Git → CI →
   ArgoCD 経路に乗せる (CHARTER §5)。

## やらないこと

- **ArgoCD の chart 更新・version 触り**。policy=manual の解錠材料集め (P-0152) とは別論点。
  本案は現在の 9.1.6 の挙動を測るだけ
- **アプリ本体の image / config 変更**。touches_apps=false。演習の main 側 commit は
  metadata ラベル等の可逆最小変更に限る
- **常設化 (毎晩 drill の CronJob 化)**。P-0094 型の常設監視への昇格は行わない。1 回の実測と
  数字の記録まで。常設したくなったら別案で提案する
- **障害時の自動復旧機構の実装**。ArgoCD 死亡を検知して直す仕組みを作る話ではない。
  測って数字に出すまで
- **dex / redis 等の ArgoCD 周辺コンポーネントの停止**。認証面の単一点は P-0151 が担う
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)

# P-0181 — application-controller は今日も OOMKill された — 根拠のない 512Mi の壁を実測系列で引き直し、「limit の何%」を常に言える計器まで引っ張る

## 目的

ops-health-report の最新 `pod_issues[0]` が `argocd-application-controller-0` の OOMKilled
(exit_code 137, finished_at 2026-08-23T08:57:03Z, restarts 4)。`apps/argocd/values.yaml` の
controller.resources.limits.memory=512Mi は出典のない推測値で、substrate 規則
「memory limits は実測の裏付けなしに付けない」(T-0055) の裏側 — 実測なしに**小さく**付けること — が事故になった形。
health ブランチには pod_metrics の履歴が日々積まれているのに、このコンポーネントの実使用量を時系列で読んで
limit を決めた者はいない。ArgoCD が落ちている間、homelab 全体のデプロイ経路と器の merge 判定が同時に失明する。
VISION 段階 2「homelab 本体の保守」の直撃。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0181` の checkout でリポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -s ops/projects/logs/P-0181/memory-evidence.md && grep -qE 'ピーク|p95' ops/projects/logs/P-0181/memory-evidence.md`
  — 数値の根拠 (どの日のどのピークか) を記した証跡ファイルが存在し、ピーク・p95 に言及していること。
  実測 rc=1 (`logs/P-0181/` に memory-evidence.md 未存在)。
- [ ] `python3 -c "import sys; s=open('apps/argocd/values.yaml').read(); b=s.split('repoServer')[0]; c=b[b.index('controller'):]; sys.exit(0 if ('resources' in c and '512Mi' not in c) else 1)"`
  — values.yaml の controller セクションに resources があり、根拠のない 512Mi が抜け替わっていること。
  実測 rc=1 (現行 limits.memory=512Mi のまま。values.yaml L56-63)。
- [ ] `python3 -m unittest ops.tests.test_argocd_memory_series`
  — 集計ロジックが fixture で固定されていること。
  実測 rc=1 (モジュール未存在、FAILED errors=1)。
- [ ] `python3 ops/tools/argocd_memory_series.py --check`
  — 集計ツールが存在し、--check モードが冪等に成功すること。
  実測 rc=2 (スクリプト未存在)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) 成長率の算出と「単調増加なら leak 疑い」の明示、(2) request≈p95×余裕 / limit≈観測ピーク×マージンという
決め方そのものの妥当性、(3) 近接警報 (実使用が limit の N% 超 → latest.json の argocd セクション、N は rules.json)
の実装、(4) 成長率が有意な場合の seeds.md への恒久策 1 行 — は worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- データ源は既にある: `origin/ops-health-report` ブランチに `ops/health/history/2026-08-05.jsonl` 〜
  `2026-08-23.jsonl` (19 日分)。1 行 1 回分の JSON で、`pod_metrics[].containers[]` に
  pod 名 `argocd-application-controller-0` / コンテナ名 `application-controller` の
  `memory` が Kubernetes quantity 文字列 ("320908Ki" 等) で載る。取得は
  `git show origin/ops-health-report:<path>` (CHARTER §2 確立経路) か、テスト容易性のため
  --dir 相当の入力受付を併せる
- 実測の方向づけ: 2026-08-23 のみでも使用量は 190588–407600 Ki で往復し、ピーク (~398Mi) は
  既に limit 512Mi の ~78%。OOMKill の証拠自体は latest.json `pod_issues[0]` に残っている。
  再起動ごとに使用量がリセットされるため「単調増加」判定は系列全体で行う (worker が実数で判定)
- report.py 側: `collect_pod_metrics()` (apps/ops-health-reporter/report.py:169) が既に
  コンテナ別 usage を集めており、latest.json には argocd 専用セクションが今は無い —
  新設して「limit の N% 超」を出す。N は rules.json に新節 (heartbeat.stale_seconds 型の
  `_comment` 付き) で置く。**rules.json は人間レビュー必須パス** (.github/CODEOWNERS:
  `/ops/rules.json @hikuohiku`) なのでこの成果 PR は auto-merge されず人間待ちになる —
  仕様どおりであり欠陥ではない。PR 本文にレビュー依頼を書いて納品扱いにする
- unittest の既存パターン: 判定は純関数に切り出し、合成 fixture で両方向 (落ちること/通ること) を固定
  (test_backup_coverage.py の docstring が規範。「今たまたま通っている」と「正しい」を区別する)。
  quantity パース (Ki/Mi/Gi/無印バイト) も純関数にして fixture で固定する
- `--check` の意味論は repo 慣習に倣う: 再計算がコミット済みの証跡 JSON と一致することの冪等検査
  (P-0124 の --check 先例)。不一致・データ欠損は沈黙せず rc!=0 で落ちる
- verify 第 2 項目は「repoServer の手前で分割 → 最初の 'controller' 出現以降」を検査する。
  controller セクションより前 (ファイル冒頭等) に "controller" を含むコメントを足すと
  検査対象スライスが変わる。根拠コメントは **controller: ブロックの中または直後に書く**

### 作り方

1. `ops/tools/argocd_memory_series.py` — stdlib のみ。履歴 jsonl を読み、application-controller の
   メモリ系列から全期間ピーク・p95・成長率 (単調増加なら leak 疑いを明記) を算出して
   stdout (人間可読) と JSON (--json 相当) に出す。I/O と集計を分離し、集計は純関数
2. 実測値から values.yaml の controller.resources を引き直す。request≈p95×余裕、
   limit≈観測ピーク×マージン。どの日のどのピークかを manifest コメントと
   logs/P-0181/memory-evidence.md に書く。成長率が有意なら evidence 側にも記録し、
   恒久策 (processors チューニング等) を seeds.md に 1 行残す
3. 近接警報: report.py (または既存チェッカー) が「実使用が limit の N% 超」を
   latest.json の新設 argocd セクションに出す。N は rules.json。limit 自体は
   values.yaml 由来の値を reporter に持たせる形 (ハードコードしない)
4. `ops/tests/test_argocd_memory_series.py` — 合成 fixture 系列 (単調増加/安定/欠損混在) で
   ピーク・p95・成長率・quantity パース・leak 判定を固定する

## やらないこと

- **ArgoCD 自体のバージョン更新・chart 更新**。別論点 (inventory 追従の管轄)
- **repoServer / server の resources 変更**。今回の事故対象は controller のみ
  (verify 第 2 項目も repoServer 手前で分割している)。同じ 512Mi でも触らない (1 PR 1 論点)
- **processors チューニング等の恒久策の実装**。メモリ成長の一次原因対策は次の立案の種として
  seeds.md に 1 行残すまで
- **HPA / 自動スケールの導入**。計器が先。単一ノードで水平分散も効かない
- **クラスタへの直接適用**。変更は Git → ArgoCD 経由のみ (CHARTER §5)
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする (CLAUDE.md)

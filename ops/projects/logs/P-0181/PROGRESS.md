# P-0181 — PROGRESS

## セッション記録

### セッション1 (2026-08-23) — 集計ツール新設と実系列の確定 (verify 3・4 を green 化)

**やったこと**

- `ops/tools/argocd_memory_series.py` 新設。origin/ops-health-report の
  `ops/health/history/*.jsonl` (`git ls-tree` + `git show`、CHARTER §2 経路) から
  application-controller のメモリ系列を集計し、ピーク (時刻付き) / p95 / 中央値 /
  日次ピーク / 成長率を出す。stdlib のみ。I/O と集計は分離し、集計は全て純関数。
  `--dir` でローカル jsonl を直接読める (テスト・オフライン用)
- `--check` は証跡 JSON に記録した観測窓 (window.first..last) **の中だけ**再計算して
  比較する窓ピン留め方式。履歴は 30 分毎に伸びるので素朴な全期間再計算は翌日必ず
  不一致になるため。窓内の書き換え・データ欠損・pod 名変更は rc!=0 で落ちる
- `ops/tests/test_argocd_memory_series.py` 新設 (21 テスト)。quantity パース /
  分位点の線形補間 / OLS 傾き / leak 判定 / 有意性判定を手計算値で固定し、CLI 結合
  試験は tempfile の合成 jsonl で git 依存なし。**未来の追記でも --check が落ちない**
  こと (append 免疫) と、窓内改竄では落ちること (沈黙しない) を両方向で実測
- 証跡 `ops/projects/logs/P-0181/memory-series.json` をツール自身の出力で生成
  (--json のリダイレクト)。作り直すときも同じコマンドで上書きすればよい

**verify 実測 (このセッション終了時)**

- #3 `python3 -m unittest ops.tests.test_argocd_memory_series` → **green** (21 tests OK)
- #4 `python3 ops/tools/argocd_memory_series.py --check` → **green** (rc=0)
- #1 (memory-evidence.md) ・#2 (values.yaml) は未着手のため引き続き failing (rc=1)

**実測系列の確定した数値 (867 サンプル, 2026-08-05T08:00:04Z .. 2026-08-23T09:30:05Z)**

| 量 | 値 |
|---|---|
| ピーク | **398.0Mi (2026-08-23T04:30:08Z)** |
| p95 | 314.9Mi |
| 中央値 / 最小 | 261.4Mi / 180.4Mi |
| 成長率 | **+15.9Ki/day** (30 日外挿 +478.3Ki) |
| leak_suspect / significant | False / False |

通常日の日次ピークは 297–349Mi で往復し、08-23 だけ 398Mi に跳ねている
(OOMKill 当日。finished_at 08:57:03Z の直前 04:30 のサンプル)。

**分かったこと**

- **成長率は有意でない** (+15.9Ki/day、30 日外挿は中央値の 0.2% 未満。閾値は
  「30 日外挿 ≥ 中央値の 10%」に実装済み)。leak 疑いも無い (単調増加ではない)。
  よって spec の「成長率が有意なら seeds.md に恒久策を 1 行」は**発火しない**。
  seeds.md への記載は見送った (条件未達での記載は帳簿を汚す)
- **観測ピーク 398Mi は真のピークの下限**。metrics-server は約 30 分間隔の瞬間値で、
  同日に 4 回 OOMKilled (旧 limit 512Mi) している事実から、真の使用量はサンプルの
  隙間で **≥512Mi に達したことが確定する**。「limit ≈ 観測ピーク × マージン」を
  素朴に適用するとこの事実を無視して再度事故る
- 次セッションへの limit 引き直しの提案 (DoD 2 の材料。判断は実装者がやること):
  request ≈ p95 (314.9Mi) の切り上げ **320Mi**、limit は「観測ピーク」と「OOMKill
  実績値 512Mi」の大きい方 (=512Mi) × 1.5 マージンで **768Mi**。CPU は触らない

**次のセッションへ (罠と手順)**

1. **verify #2 の罠**: `'512Mi' not in c` の判定で、c は「repoServer の前で分割 →
   最初の 'controller' 出現以降」。つまり **controller ブロック内のコメントに
   リテラル "512Mi" を書くと即落ちる**。旧 limit に言及するときは `0.5Gi` と書くこと
   (例: 「旧 limit (0.5Gi) で 2026-08-23 に 4 回 OOMKill」)。また controller: ブロックの
   **前に** 'controller' を含むコメントを足すとスライス起点がずれるので、根拠コメントは
   controller: ブロックの中または直後に置く (PROJECT.md 前提節と同じ注意)
2. 数値の根拠は memory-series.json と上表。manifest コメントには
   「ピーク 398.0Mi @ 2026-08-23T04:30:08Z / p95 314.9Mi / 867 サンプル
   2026-08-05..23 / OOMKill 実績 ≥512Mi」を書き、memory-evidence.md (#1) にも
   同根拠を記す (grep 対象語「ピーク」「p95」を必ず含む)
3. **近接警報 (DoD 3) の調査済み事実**: reporter は in-cluster で動き rules.json を
   直接読めない。configMapGenerator は kustomization.yaml の外のファイルを読めない
   ため (version_watch.py の二重管理先例)、閾値 N の配線は「rules.json 由来の値を
   アプリ dir に同期コピー + CI 同期チェック (check_download_ledger_script_sync.py 型)」
   が repo 慣習に合う。一方 **limit 自体はハードコードしなくてよい**:
   `/api/v1/namespaces/argocd/pods/argocd-application-controller-0` の
   `spec.containers[].resources.limits.memory` から実機の値を取れば values.yaml 由来が
   保証され、values.yaml を引き直しても reporter 側の追従作業が消える
   (reporter RBAC は pods get 済み)。reporter の ConfigMap は
   `disableNameSuffixHash: true` だが CronJob なので各 run で最新 CM を mount し直す
   — rollout 追配は不要
4. --check を将来回したとき履歴が伸びても落ちない (窓ピン留め)。より新しい窓で
   証跡を作り直したくなったら `--json > ops/projects/logs/P-0181/memory-series.json`
   で上書きコミットするだけ

## 引き継ぎ事項

- 受入チェックリストは #3・#4 が green、#1・#2 が failing。残りは
  values.yaml 引き直し + memory-evidence.md (→ 上記 1・2) と近接警報 (→ 上記 3)
- ツールの既知死角は argocd_memory_series.py の docstring に記載済み
  (観測ピークは下限 / 鋸歯状 leak は slope でしか拾えない / pod 名決め打ち)

## 発見 (spec 外。curriculum の原料として記すだけ)

- なし (今回の範囲では出なかった。近接警報の配線制約は上記 3 に実装前提として記録済み)

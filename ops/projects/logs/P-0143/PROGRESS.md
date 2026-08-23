# P-0143 PROGRESS

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### 2026-08-23 セッション1 — 受入1項目目 (収集スクリプト新設) を green に

**やったこと**: `ops/tools/coder_idle_audit.sh` を新設。verify 1項目目
`bash -n ops/tools/coder_idle_audit.sh` が green。`--self-test` (クラスタ不要の
組み込み fixture で全経路を通すモード) も green で、verify 2項目目と同じ python
条件 (workspaces の各要素に cpu_usage / classification、top-level に reclaimable)
を出力に対して機械確認済み。verify 2・3項目目は未着手 (実データと文書なので
スクリプト単体では作れない)。

**設計で決めたこと (次セッションはこの前提の上に立つこと)**:

- workspace の識別はテンプレートのラベルで行う:
  Pod `app.kubernetes.io/name=coder-workspace` / PVC `app.kubernetes.io/name=coder-pvc`
  (apps/coder/templates/personal/main.tf より)。制御プレーン Pod (`app=coder`) は
  ラベル系が別なので selector で最初から排除される。
- 出力は `-o` (既定: repo root の ops/projects/logs/P-0143/idle-audit.json、git 外なら
  rc=64)。schema は `coder-idle-audit/v1`。書き出しは同 dir の mktemp + os.replace で原子。
- 分類ルール: `kubectl top pod` を SAMPLES 回取り、平均 < CODER_AUDIT_IDLE_CPU_M
  (既定 50m) かつ 最大 < CODER_AUDIT_IDLE_CPU_MAX_M (既定 500m) → idle。
  metrics 不取得時は unknown (捏造しない)。生サンプル・閾値・根拠を
  classification_basis に残すので人間は後から再判定できる。
- reclaimable は requests_based (capacity 差分) と usage_based (観測窓の実解放) の両方。
  PVC が idle 分に合算されるのは requests_based.pvc_gib のみで、note に「Pod を止めても
  local-path のディスクは空かない。PVC 削除が前提で不可逆」と明記済み。
- 終了コード: 0=収集成功 (部分的な欠損は collection_notes に記録して 0)、2=到達不能/
  認証失敗、3=self-test 失敗、64=使い方誤り。

**分かったこと / 罠**:

- **この checkout にはクラスタ credential が無い** (PROJECT.md 前提の再実測):
  `KUBERNETES_SERVICE_HOST=10.43.0.1` は設定され API server にも到達するが、
  `/var/run/secrets/kubernetes.io/serviceaccount` が未マウントで anonymous 401。
  doppler / just / tailscale バイナリも無い。→ 実データ収集はこの環境では不可能。
  スクリプトはここでは rc=2 で正直に落ちる (実測済み)。
- kubectl top は `-l <selector>` を受ける (Pod 全行が 1 呼び出しで返る)。
  fixture もそれに合わせてある。
- exec による df (`kubectl exec ... -- df -k -P /home/coder`) は pods/exec 権限が
  要る。autopilot-reader 系 SA (get/list のみ) だと拒否される可能性が高く、その場合
  used_gib=null + collection_notes 記録になる。拒否される見込みが高いなら最初から
  `--no-exec` を付ける (タイムアウト待ちが減る)。代替経路 (nodes/stats/summary proxy 等)
  は未検証 — 権限が通るか次セッションで実測すること。
- metrics.k8s.io への get 権限も reader ClusterRole で通るか未実測。top が全滅すると
  分類は全部 unknown になり verify#2 は workspaces 非空のまま通るが中身が貧弱になる
  (reclaimable の usage_based が 0 扱い)。その場合は権限の話を PROGRESS 発見節に残すこと。

**次のセッションへの一言**: verify#2 に着手。まず runner Job 内 (kubeconfig or SA token
のある環境) で `ops/tools/coder_idle_audit.sh -s 5 -i 10` を実行して
idle-audit.json を作り commit する。exec/metrics の権限が無ければ `--no-exec` と
unknown 分類で正直に畳み、欠損内容を collection_notes に任せる。分類閾値 (50m/500m) が
実データに対して妥当かは JSON の samples 見てから判断 (変えるなら env で上書きして
根拠ごと記録)。verify#3 の docs/coder-idle-policy.md は実測表が必要なので #2 の後。
autostop 対処案は main.tf L84-87 の `home_disk_size` ではなく Coder の workspace
template パラメータ (time_to_stop 相当) の話として書くこと。実装自体は別プロジェクト。

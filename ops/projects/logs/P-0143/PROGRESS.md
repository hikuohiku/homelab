# P-0143 PROGRESS

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## 人間への依頼 (このプロジェクトを完了させる唯一の外注。器では実行できない)

実データ収集にはクラスタ credential が必要だが、worker 環境には無いことが
3 セッション分実測済み (kubeconfig 無し / SA token 未マウント / doppler・tailscale 等
バイナリも無し / JWT 形式のファイルも探索済みで無し)。**以下を credential のある環境で
1 回実行し、産出された JSON をこのブランチに commit & push してほしい。** それだけで
verify#2 が閉じ、レビューに進める。

```bash
git switch project/p-0143                  # 出力先が repo 内に決まっているためブランチ上で
ops/tools/coder_idle_audit.sh -s 5 -i 10   # 所要 1〜2 分 (kubectl top を 5 回サンプリング)
git add ops/projects/logs/P-0143/idle-audit.json
git commit -m "P-0143: idle-audit 実測結果"
```

- 期待: rc=0 と「OK: ... へ書き出し」。admin kubeconfig なら pods/exec・metrics.k8s.io も
  通るはずなので、実使用量 (メモリ/PVC) まで取れる
- `collection_notes` に欠損が出てもそのまめてよい。欠損を正直に残すのが設計
- ブランチを切りたくない場合は `-o` で任意の場所へ出力し、JSON を issue #56 に貼っても
  よい。次の worker セッションが実成果物パスへ転記する (人の生データの写しであり捏造ではない)
- **`--self-test` は必ず `-o` を付けて動かすこと** (-o 無しだと fixture を実成果物パスへ
  書こうとして rc=64 で拒否するガード入り。2026-08-23 追設)
- 停止中 workspace があるほど `reclaimable.stopped_pvc_gib` にディスク残留が映る。
  stop 済み workspace の PVC 残量も見たいので、消さずにそのまま計ってほしい

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

### 2026-08-23 セッション2 — verify#3 を green に (実測表は正直な空節)

**やったこと**: `docs/coder-idle-policy.md` を新設し verify#3 を green 実測。
(b) autostop 対処案+暫定推奨閾値、(c) 器の足場の除外条件は実装で書き切った。
(a) 実測表は「未計測」と明記した空節にし、idle-audit.json からの転記手順と
列定義 (schema v1 対応) だけを置いた — 数字の捏造はしない。verify#2 は本日も
この環境に credential が無く rc=2 を再実測 (前セッションと同一環境)。唯一の
failing 項目のまま。

**分かったこと / 罠**:

- **Coder の autostop の活動判定はセッションベース** (公式 docs, 2026-08-23 閲覧:
  code-server/VS Code Remote・JetBrains・web terminal・`coder ssh`・Coder Tasks の
  agent status)。`kubectl exec` も workspace 内の背景プロセスも活動と数えられない。
  → スクリプトの CPU ベース分類と Coder の idle 判定は**両方向にズレる**
  (SSH 張りっぱなしだと autostop が効かない / exec だけの無人ジョブは止まる)。
  docs §3 の除外条件はここから導いた。
- 正確な設定 CLI は `coder templates edit personal --default-ttl <duration>`
  (UI 名 "Default autostop") + `--activity-bump` (既定 1h) + `--autostop-reminder`。
  **"time_to_stop" という名のテンプレートパラメータは実在しない** (前セッション末尾の
  「time_to_stop 相当」は不正確だった訂正)。default-ttl は既存稼働中 workspace に
  遡及しない (公式注記)。強制定期再起動 (autostop requirement)・quiet hours・dormancy
  は Premium 機能で OSS では使えない → 「セッション放置への強制停止」は作れない。
- apps/coder/rbac.yaml の coder SA は coder ns 内 pods/pvc/deployments の全権限だが
  **metrics.k8s.io も pods/exec も無い**。autopilot-reader (get/list only) も同様。
  収集をどの identity で動かすかで top/exec の成否が決まる — 権限の実測は未着手。
- 器の常駐系 (autopilot 本体・ops-dashboard) は coder workspace 外 (autopilot ns の
  Deployment/CronJob) を確認した (apps/autopilot 配下に coder への言及なし)。
  docs §3 の原則「常駐系を workspace に入れない」は現状と整合している。

**次のセッションへの一言**: verify#2 が残り。credential のある環境 (kubeconfig or
SA token) で `ops/tools/coder_idle_audit.sh -s 5 -i 10` → idle-audit.json を commit →
docs/coder-idle-policy.md §1 の表へ転記して「草案」を外す、が完了の形。
どの SA でも metrics/exec が通らなければ `--no-exec` + unknown 分類で正直に畳み、
docs §1 の PVC 使用 GiB 列も「未計測 (権限)」と明記すること。分類閾値 50m/500m の
妥当性判断は JSON の samples 見てから (変えるなら env 上書きで根拠ごと記録)。
docs §2 の閾値 8h は暫定値なので、夜間アイドルの実パターンが出たら改訂検討。

### 2026-08-23 セッション3 — verify#2 の器側を完成させ、人間依頼へ切り替え

**やったこと**: verify#2 (実データ) は本日も credential 無しで収集不可能を再実測
(kubeconfig fallback の localhost:8080 接続拒否 → rc=2 を 0.25 秒で fail、成果物は
書かないことを確認)。そこで (1) 収集スクリプトの実質バグ 2 件を修正し、(2) P-0027 前例に
従い PROGRESS 冒頭へ「人間への依頼」を置いた。verify#1/#3 は green 維持、self-test は
修正込みで green (`-o $(mktemp)` 付き。実パスへの self-test は rc=64 ガードを確認)。

**修正したバグ (どちらも main.tf 実装との突合で発見)**:

- **停止中 workspace の PVC が数えられていなかった。** workspace テンプレートの PVC
  リソースには deployment と違い `count = start_count` が無く (main.tf L208 vs L239)、
  Pod を止めた workspace の home PVC は確保されたまま残る。旧実装は Pod 主導ループなので
  これらを捉えられず、「アイドルで何を奪われているか」のディスク側を過小評価していた。
  classification `stopped` エントリ + `reclaimable.stopped_count` /
  `stopped_pvc_gib` (requests_based へは合算しない — 削除は不可逆なため) を追加し、
  fixture に Pod 無し PVC を足して self-test で固定した
- **--self-test が実 idle-audit.json を fixture で上書きし得た。** 既定出力先が実成果物
  パスそのものなので、-o 無しで self-test を回すと捏造ファイルが完成する時限爆だった。
  実パスなら rc=64 で拒否するガードを追加

**分かったこと / 罠**:

- スクリプトの前提は main.tf と全部一致することを実確認済み: ラベル
  (`coder-workspace` / `coder-pvc`)・コンテナ名 `dev`(単一コンテナ)・マウント
  `/home/coder`。requests は全 workspace 固定 **250m / 512Mi**、limits はパラメータ
  (CPU 2/4/6/8 cores, メモリ 2/4/6/8Gi)。つまり requests_based の capacity 差分は
  「稼働 workspace 数 × 250m/512Mi」で上限が先に見えている
- 本環境の kubectl は kubeconfig 無しだと localhost:8080 に落ちる。session1 記録の
  「KUBERNETES_SERVICE_HOST 宛て anonymous 401」と失敗フェーズが違うが (kubectl は
  token ファイル無しでは in-cluster 設定を使わない)、credential 不在という結論は同じ。
  ~/.kube・/var/run/secrets・JWT 形式ファイルの再探索でも何も無し
- **/tmp/opencode は root 所有で autopilot から書けない** (touch で Permission denied)。
  一時ファイルは素の mktemp (/tmp 直下) を使うこと
- P-0027 は同じ「器では実行できない」問題を PR 冒頭の人間依頼で前進させたが、P-0078 は
  解決者不在のまま 20 セッション滞留した。P-0143 は前者の型に乗って依頼を冒頭に置いた —
  後者の型に戻さないこと

**次のセッションへの一言**: **収集の再試行を繰り返さないこと。** credential は無いので
rc=2 以上の情報は出ない (3 セッション実測済み)。基本はレビュー指摘への対応のみ。
もし環境が変わって SA token / kubeconfig が生えていたら冒頭の依頼コマンドを自分で
実行してよい (-o 不要。その場で idle-audit.json が正しく生成される)。人間が #56 等に
JSON を貼った場合は実成果物パスへ転記してよいが、generated_at と collection_notes は
原文のまま維持すること (出所を消すのは捏造の一種)。実測 JSON が入ったら docs §1 の表
転記と「草案」外し、分類閾値 (50m/500m) の妥当性判断までが次の仕事。

### 2026-08-23 セッション4 — 人間への依頼を #56 へ投稿 (PR が無くて依頼が見えていなかった)

**やったこと**: レビュー指摘は空、verify#2 は外部データ待ち。まず環境の credential を
env・パスの確認のみで再確認 (無し。収集の再試行はしていない)。そのうえで
(1) `AUTOPILOT_GITHUB_TOKEN` (worker 環境の env) + curl/python3 で GitHub API が叩ける
ことを実測し、#56 のコメント全 178 件を取得 → **P-0143 の JSON 投稿はまだ無い**ことを確認、
(2) `project/p-0143` の PR が**まだ 1 本も存在しない**ことを API で実測
(wrapper は verify 全 green まで PR を作らない)、(3) 投稿前に self-test を
`-o $(mktemp)` で再実行し green を確認、(4) P-0144 同型の**収集依頼を #56 へ自己投稿**した:
https://github.com/hikuohiku/homelab/issues/56#issuecomment-5384240492

verify は v1/v3 green 維持、v2 は idle-audit.json 未存在で failing のまま (期待どおり)。

**分かったこと / 罠**:

- **PR は verify 全 green まで作られない = ブランチ上の「人間への依頼」節は人間に見えない。**
  セッション3 が P-0027 型の依頼を PROGRESS 冒頭に置いたとき、「PR 冒頭」と思っていた場所は
  実際には誰も見ないブランチだった。外注が必要なプロジェクトは **#56 への自己投稿が必須**
  (P-0027 の型を真似るなら PR 経由で見える形まで含めて真似ること)
- **gh CLI は無くても GitHub API は叩ける。** worker 環境の `AUTOPILOT_GITHUB_TOKEN` は
  Issues/PR/Contents の write 持ち (#56 の過去コメントでの実測記載あり)。
  次セッションから人間の返答を自分で確認できる (wrapper の渡してくれる文脈を待たない)
- #56 の直近は同型の外注 2 件が未回収 (P-0118 Telegram 送信依頼 01:23、P-0144
  tailscale devices.json 依頼 04:09)。人間がまとめて処理する可能性があるので急かさない。
  自分の投稿は 04:37

**次のセッションへの一言**: 収集の再試行はしないこと (credential 無しは 4 セッション実測)。
**最初に #56 を API で見ること**:

```bash
curl -s -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN" \
  "https://api.github.com/repos/hikuohiku/homelab/issues/56/comments?per_page=100&page=2" 
```

(page 1 が最古 100 件、page 2 に 2026-08-11 以降の最近分。自分の投稿
issuecomment-5384240492 以降に idle-audit.json の JSON 貼りがあるか探す)
JSON が貼られていたら原本として `ops/projects/logs/P-0143/idle-audit.json` へ復元
(generated_at / collection_notes は原文のまま。出所を消すのは捏造の一種) →
docs §1 の表へ転記し「草案」を外す → 分類閾値 (50m/500m) の妥当性を samples で判断。
ブランチ直接 push の場合もあるので `git fetch origin && git log origin/project/p-0143
--oneline -3` も併せて確認。返答が無ければ何もしないで終わってよい (待ちは正当な状態)。
レビュー指摘が来ていたらそれを最優先。

### 2026-08-23 セッション5 — 返答待ちの確認のみ (変化無し)

**やったこと**: 指示どおりの 2 点確認だけして、何もせずに終えた。

1. **#56 に返答は無い**。API で page 2 を取得し、自分の投稿
   issuecomment-5384240492 (04:37) 以降のコメントは **0 件**を実測。
   idle-audit.json の JSON 貼りも無し。
2. **`origin/project/p-0143` に直接 push も無い** (`git fetch` 後、先頭は依然
   58767376 = セッション4 の commit)。fetch で動いたのは ops-state / p-0116 /
   p-0139 のみで本件と無関係。

環境の credential 再確認もパス存在チェックのみで実施 (~/.kube/config 無し /
SA token 未マウント / env の KUBERNETES_* は in-cluster 由来だが token ファイルが
無いので kubectl は使わない)。**credential 不在は 5 セッション目の実測。**
収集スクリプトへの再試行はしていない (rc=2 以上の情報は出ないため)。

verify は v1/v3 green 維持、v2 は外部データ待ちのまま failing (期待どおり)。

**次のセッションへの一言**: 状態はセッション4・5と完全に同じ — **#56 の返答を待つのが
正しい状態**。上の curl と `git log origin/project/p-0143` の 2 点確認だけでよく、
返答が無ければ記録だけ残して終わってよい (このセッションがその型)。急かす投稿は
しなかった (#56 には同型の外注未回収が他にもあるので、人間がまとめて処理するのを待つ)。
JSON が貼られていたらセッション4の指示のとおり原本復元 → docs §1 転記 → 閾値判断。
レビュー指摘が来ていたら最優先。

### 2026-08-23 セッション6 — 返答待ちの確認のみ (変化無し)

**やったこと**: セッション5と同じ型の 2 点確認だけして、記録を残して終えた。

1. **#56 に返答は無い**。API page 2 を取得し、自分の投稿 issuecomment-5384240492
   (04:37) 以降は **0 件**。並び昇順の実測で自分の投稿が #56 全体の最終コメント
   (全 179 件 = セッション4 時点の 178 件 + 自分の投稿)。JSON 貼りは無し。
2. **`origin/project/p-0143` に直接 push も無い** (`git fetch` 後、先頭は依然
   58767376)。fetch で動いたのは ops-state / p-0116 のみで本件と無関係。

credential 再確認はしていない (5 セッション実測済みで再計測の価値なし)。
収集スクリプトへの再試行もしていない。

verify は v1/v3 green 維持、v2 は外部データ待ちのまま failing (期待どおり)。

**次のセッションへの一言**: 状態はセッション4〜6で完全に不変 — **#56 の返答を待つのが
正しい状態**。最初に curl (page 2) と `git log origin/project/p-0143` の 2 点確認だけし、
返答・push が無ければこのセッションと同じく記録だけ残して終わってよい。
JSON が貼られていたら原本復元 (`generated_at` / `collection_notes` 原文維持) →
docs §1 表転記と「草案」外し → 分類閾値 (50m/500m) の妥当性判断まで進む。
レビュー指摘が来ていたら最優先。

# P-0196 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。
文脈は PROJECT.md とこのファイルと git log のみ。

## セッション 1 — 2026-08-23: lab ツール実装 + verify 第 1 項 green + 前提の実機実測

### やったこと

`ops/projects/scripts/argocd_oom_lab.py` を新設した。サブコマンド構成:
`--plan`(オフライン計画) / `up`(構築) / `sample`(1 回計測→CSV 追記) / `status`(要約) /
`verdict`(3 分類判定→verdict.json) / `down`(削除)。rendered 済み manifest
(`rendered/argocd-{916,1040}.yaml`, 各 27 オブジェクト) と values
(`rendered/lab-values.yaml`) もコミット済みで、**ランタイムに helm 不要**。
chart tgz の sha256 も実ダウンロードして pin した (9.1.6: 204270 bytes /
`3ff4f2b2…e4fe8`、10.4.0: 233772 bytes / `5abb71c1…b971d5`)。

**verify 第 1 項を自分で回して green**: `python3 ops/projects/scripts/argocd_oom_lab.py --plan`
rc=0、クラスタ非接触 (ネットワークも使わない)。
verify 第 2 項 (verdict.json) は未達のまま — 実測が必要なため。分類器ロジックは合成 CSV
フィクスチャで 6 シナリオ全 green (両系統リーク→leak / 片側のみリーク→chart-regression /
平坦&平台値≥460.8Mi→insufficient-request / 平台値乖離≥25%→chart-regression /
低負荷平坦→rc=2 拒否 / restart 増加→リーク様相扱い)。

### 実測した前提 (PROJECT.md の記載を更新・補強)

- **権限マトリクス (kubectl auth can-i, SA=autopilot:autopilot-writer)**:
  create/delete namespaces **yes** / create secrets **no** / roles, rolebindings,
  secretstores **no** / jobs, deployments, statefulsets, services, externalsecrets **yes**。
  namespace 作成可は PROJECT.md の「未実測」を実測で解決。
- **ESO 経路は実証済み**: probe namespace を作り ExternalSecret
  (ClusterSecretStore doppler 参照) を適用 → **t+10s で Ready=True**、検証後 probe ns は
  削除して残置ゼロを確認済み。これが lab の Secret 供給方式。
- **ESO webhook の制約**: data/dataFrom 空は admission deny
  (`either data or dataFrom should be specified`)。回避として実在 Doppler キー
  (`DEX_ARGOCD_CLIENT_SECRET`) への remoteRef を 1 本置くが、template.data を明示すると
  最終 Secret には template 固定値しか入らない。**argocd-secret/argocd-redis の値は
  ダミー固定値で実 credential ではない**。
- **node01 は逼迫気味**: `kubectl top nodes` → CPU 86% / MEM 83% (10013Mi)。空き頭数は
  ~2Gi。lab controller limits は 1Gi×2 とした (被験体を殺さない上限かつノード圧迫の下限)。
  preflight は MEM>95% で中断、>90% で警告する。
- **本番 Application 数は 16** (`kubectl get applications -n argocd | wc -l`)。
  合成負荷 30 本/系統はその約 2 倍。
- **名前空間 Role では Deployment 書き込み不可** (render 実測: get/list/watch のみ)。
  sync を実行させようがない → **syncPolicy manual で refresh→generate→diff→status の定常
  ループだけを計測対象にした**。sync 時のメモリスパイクは再現できない限界であり、verdict
  の excluded_by_design と notes に明記される。prod OOM が「数時間稼働後 restarts 4」という
  経過であることとは整合する。
- **chart 9.1.6 に server.enabled / applicationSet.enabled スイッチは存在しない**
  (helm show values 実測。dex/notifications/redis にはある)。post-render フィルタ
  (app.kubernetes.io/name ラベル基準) で Deployment+Service を落として対処。
- **NetworkPolicy は新チャートのみ 4 個付属** → 新旧非対称になるため双方で不使用。
- **argocd-repo-server-tls Secret は作らなくてよい**: chart がこのボリュームを
  optional: true で mount する (chart 標準) ので pod 起動は阻害されない。
  ただし通信モードが version 既定値に振られないよう
  `configs.params.controller.repo.server.plaintext: "true"` を明示した。
- argocd-redis の REDIS_PASSWORD 参照は optional: false (render 実測コメント) —
  pod 起動前に Secret 存在が必須。up は ESO Ready 待ちを挟むので順序は担保される。

### 発見 (仕様外。curriculum が拾うこと)

- runner コンテナの `/tmp/opencode` は root 所有で書き込めない
  (`mktemp -d /tmp/opencode/...` → Permission denied)。素の `mktemp -d` は使える。
- helm バイナリはイメージに無いが get.helm.sh v3.16.4 の到達性は再確認済み
  (今回は rendered をコミットしたので次セッションは不要のはず)。
- filter_manifest 初版に `-server` suffix 誤マッチで repo-server まで落ちるバグがあり、
  実データ通しで発見・修正した (ラベル基準に変更)。レンダリング物は必ず kept 一覧を
  目視すること。

### 次セッションへの引き継ぎ

1. **最初に `python3 ops/projects/scripts/argocd_oom_lab.py status`** — CSV があれば途中経過、
   無ければ `up` から。up の preflight (can-i / node メモリ / CRD 存在) は自動で走る。
2. サンプリングは 15 分間隔 × 4 時間窓 (各系統 ≥8 サンプルで verdict 可能)。
   **セッション跨ぎ前提なので、各セッション冒頭で sample を 1 回叩いて CSV を commit**
   すること。長時間 sleep での待ち合わせはしない (親 shell ごと死ぬ罠 — PROJECT.md 既記述)。
   verdict に必要なサンプルが揃うまで複数セッションかかる想定。
3. `verdict` は判定不能なら rc=2 で拒否する (無理な 3 分類当てはめをしない設計)。
   rc=2 のメッセージに従って負荷量/窓を見直す。**verdict.json を手で捏造しないこと**。
4. 全サンプル収集後は `down` で namespace 削除 + 残置ゼロ確認。
   **CRD は本番共有のため絶対に削除しない** (down も削除しない)。
5. up 直後は pod が ImagePull 中の可能性がある。sample の phase 列 Running 以外が続く場合、
   `kubectl describe pod -n argocd-lab-916` で原因確認 (secret 未同期なら ESO 待ち)。

## セッション 2 — 2026-08-23: up を実行 → RBAC の壁に到達。人間の一手を要求する形で中断

### やったこと

1. 引き継ぎどおり `status` → CSV 無しを確認 → `up` を実行した。
2. **up は preflight で即失敗** (CRD 3 種が「存在しない」判定)。これは誤検知だった:
   autopilot-writer には CRD の get/list 権限すら無く、`kubectl get crd` の Forbidden を
   「不存在」と解釈していた。discovery API (`kubectl api-resources --api-group=argoproj.io`)
   なら全認証済み主体に見えるので、preflight をそちらに修正して green。
   実際 `applications/appprojects/applicationsets.argoproj.io` は存在する
   (本番 argocd ns の Application 16 本が見えていることでも裏取り済み)。
3. 再実行の up は ESO 待ちまで成功 (**両系統 4 本の ExternalSecret が全て SecretSynced、
   セッション 1 の probe 実測を lab ns でも再確認**)。その直後の rendered manifest 適用で
   `kubectl apply` が ServiceAccount / Role / RoleBinding の GET で Forbidden — ここで頓挫。
   (apply は create/patch の判断に GET を要する。kubectl はエラー後も残りオブジェクトの適用を
   続けるので redis Deployment 等は部分的に立った)
4. 中断を確定させて `down` を実行、lab namespace 2 個削除と残置ゼロを確認した。
   失敗した up が書いた `lab-state.json` は削除 (成功していない状態の記録が残ると
   verdict の source_sha 参照が汚れるため)。

### 分かったこと (壁の正体)

`apps/autopilot/rbac.yaml` を読んで権限の出所を確認した。autopilot-writer ClusterRole は
pods/deployments/jobs/configmaps/namespaces/applications/externalsecrets 等に verbs:* を持つが、
**serviceaccounts・roles・rolebindings・secrets は含まれない**。コメントに意図が明記されている:

> rbac.authorization.k8s.io: 自分の権限を自分で広げる経路を作らない

つまりこれは障害ではなく設計である。chart 由来の application-controller は自前 SA + Role なしでは
Applications を list/watch できず、reconcile ループ (refresh→generate→diff→status) が成立しない。
default SA のまま動かしても RBAC エラーのループを計測するだけで、RSS 曲線は無意味になる。

検討して棄却した代替案 (将来のセッションが再考査する無駄を省くため):

- **lab pods への writer SA 注入**: SA は pod と同一 namespace 必須。autopilot ns の SA は
  使えない。そもそも新規 SA が作れない時点でアウト。
- **runner 自身のトークンを env で lab controller に渡す** (kubeconfig + bearer token): 技術的には
  可能そうだが、writer credential を意図的に複製して別 workload に持たせる行為であり、
  「疑わしければやらずに」に該当。単独では判断しない。
- **prod argocd に合成 Application を足して prod controller を直接計測**: spec が
  本番 argocd namespace への変更を明示禁止 (既に OOMKilled 中の被験体をさらに追い込む)。
- **RBAC エラーループのまま計測**: 曲線の意味が変わる。verdict の捏造に近い。

### 発見 (仕様外。curriculum が拾うこと)

- kubectl apply は一部オブジェクトが Forbidden でも rc=1 で最後まで適用を続ける。
  「apply 失敗 = 何も出来ていない」ではない。部分適用の掃除を忘れやすい。
- CRD の get/list ができない主体にとって `kubectl get crd <name>` の失敗は存在否定にならない。
  存在確認は discovery (`api-resources`) 経由で行うべき。今回スクリプトを修正済み。

### 人間への依頼 (これがこのセッションの主産物)

`ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml` を用意した。
**自動適用はしない。** 内容: lab ns 2 個 × (SA x2 + Role x1 + RoleBinding x1)、すべて
名前空間スコープ、secrets 不含、applications の create/delete 不含 (投入は writer が既存権限で行う)。
最小権限の根拠はファイル冒頭のコメントに書いた。適用は 1 コマンド:

```
kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml
```

削除作業は不要 (down の Namespace 削除に巻き込まれて消える)。適用されたら次セッション以降で
(1) rendered manifest の serviceAccountName を提案 YAML の SA 名に合わせる後処理を追加、
(2) 各 ns に destinations を自 ns 限定した AppProject `default` を作る (appprojects は writer に
verbs:* 済み。chart は default project を作らないため、無いと reconcile が project 検証で
短路する恐れ)、(3) up 再実行、という順序。

### 次セッションへの引き継ぎ

1. **まず proposed-rbac-for-human.yaml が適用済みか確認すること**:
   `kubectl get sa argocd-lab-application-controller -n argocd-lab-916` が通れば適用済み。
   通らなければ本プロジェクトは人間の一手待ち。勝手な回避策 (トークン複製等) を打たないこと。
2. 適用済みなら上記 (1)(2)(3) を実装して up。up 後はセッション 1 の手順どおり
   sample → commit の繰り返しに戻れる。
3. サンプリング期間中は lab namespace を消さない (人間権限の SA/RBAC も一緒に消えるので
   再適用が必要になる)。down は全サンプル収集後のみ。
4. verify 第 1 項 (--plan) は本セッション終了時点でも green (preflight 修正後に再実行済み)。

## セッション 3 — 2026-08-23: 人間適用の有無を権限内で自動判定できるようにし、up を「適用された瞬間に一本通る」状態まで完成させた

### やったこと

1. 引き継ぎどおり RBAC 適用状況を確認したところ、**前セッションの引き継ぎに書かれた検出方法
   (`kubectl get sa ...` が通れば適用済み) は原理的に不可能だった**: autopilot-writer は
   serviceaccounts の get 自体を持たないため、**適用済みでも常に Forbidden になる**。
   実際の適用判定は `kubectl get ns argocd-lab-916` (writer は namespaces verbs:*) で行い、
   **未適用 (lab ns 非存在) を確認**。ついでに can-i を全域で実測し直した:
   serviceaccounts / roles / rolebindings / secrets の create・get がすべて no、さらに
   localsubjectaccessreviews の create も impersonate も no。→ SA 存在の直接・間接確認は
   ほぼ全手段が閉じている。
2. そこで **admission probe** を実装: SA 参照の Pod を 1 個作らせ、存在判定を ServiceAccount
   admission プラグインに委ねる (SA 無ければ create 自体が拒否され 'serviceaccount ...
   not found' が返る)。これを `up` 冒頭に組み込み、未適用なら自分が今作った空 ns を掃除して
   適用コマンド付きで即中断するようになった。
3. 前セッションの残課題 (1)(2)(3) をすべてスクリプト側に実装:
   - rendered 27 docs 中 writer が適用できるのは **12 docs** (ConfigMap7/Service2/
     Deployment2/STS1)。SA5/Role5/RoleBinding5 は人間適用分と重複するため **apply 時に除外**
     (除外しないと apply の GET Forbidden で部分適用が残る — セッション 2 実測の再発防止)。
   - chart 由来 STS の `serviceAccountName: argocd-application-controller` を提案 YAML の
     SA 名 (`argocd-lab-application-controller`) に apply 時に書換。逆 (提案ファイルを直す)
     にしなかったのは提案が既に「提案済み」だから。残存参照が許可集合
     {default, argocd-lab-application-controller, argocd-lab-repo-server} 外なら assert で落とす。
   - AppProject default x2 を up が生成 (destinations 自 ns 限定 + clusterResourceWhitelist 空
     = cluster-scoped 拒否)。preflight に appprojects 作成権限チェックも追加。
   - lab-state.json の書き込みを probe 通過後へ移動 (未適用中断時に state が残って verdict の
     source_sha 参照を汚す — セッション 2 の後始末で判明した問題の構造的解消)。
4. 自己テストを実機で実施: --plan オフライン green (rc=0) / probe 負例 (SA 無し ns で期待
   メッセージどおり拒否・pod 残置なし) / **up 本番経路の中断動作** (ns 作成→probe 失敗→空 ns
   掃除→rc=1、lab-state.json 未書込、残置ゼロ)。テスト用 temp ns (p0196-probe-selftest) は
   削除済み。

### 分かったこと

- **Forbidden ≠ 不存在の罠は CRD (セッション 2) だけではなかった**。「get できない資源の
  存在確認」が必要になったら discovery か admission など、別の面から当たるしかない。
  存在確認手段を持たないチェック項目を引き継ぎに書くのは次セッションの時間を溶かす。
- kubectl create は apply と違って GET を要しない。ただし今回の壁 (RBAC リソース群) は
  create 権限自体がないのでこの差は救いにならず。権限不足の切り分けでは
  「apply 失敗 = get 不足かもしれない」「create 失敗 = 真に create 不可」と分解して考える。
- pause イメージ等の Pod を 1 個建てる admission probe は「リソース X の存在確認権限がない」
  問題の汎用解になりうる (X を参照するオブジェクトを作れるなら)。

### 次セッションへの引き継ぎ

1. **RBAC 適用の有無は自分で判定しない。そのまま `python3 ops/projects/scripts/argocd_oom_lab.py up` を叩く。**
   適用済みなら ESO 待ちまで進んで Application 投入まで完了する。未適用なら空 ns を自力で
   掃除して rc=1 で中断し、人間への適用依頼メッセージを出す (残置ゼロ確認済みなので
   後片付け不要)。回避策は打たないこと (セッション 2 の決定を引き継ぐ)。
2. up 成功後はセッション 1 の手順に戻る: sample (--note 付き) → CSV commit の繰り返し。
   15 分間隔 × 4 時間窓、各系統 ≥8 サンプルで verdict。**セッション跨ぎ前提**で、
   各セッション冒頭で sample 1 回叩いて commit すること (長時間 sleep での待ち合わせ禁止)。
3. sample の phase 列が Running 以外続きなら `kubectl describe pod -n argocd-lab-916` で原因確認。
   controller が CrashLoop する場合、まず RoleBinding の subjects と workload の
   serviceAccountName の不一致を疑う (本セッションで書換済みだが、chart 再 render 時は要再確認)。
4. verify 第 1 項 (--plan) は本セッション終了時点でも green。第 2 項 (verdict.json) は
   サンプリング完了まで failing のままで正常。
5. status サブコマンドが lab ns の有無も表示するようになった (CSV 無しでも診断に使える)。

## セッション 4 — 2026-08-23: up 実機実行で「未適用」を実測 (人間の一手は未だ)。部分適用時の残留穴を塞ぐ

### やったこと

1. 引き継ぎどおり **RBAC 適用の有無は自分で判定せず `up` をそのまま実行**した。
   結果: admission probe が未適用を正しく検出 → 自分で作った空 ns を自力掃除 → rc=1 で中断
   (セッション 3 で実装した経路の実機再確認)。残置ゼロを実測:
   `kubectl get ns argocd-lab-916 argocd-lab-1040` 両方 NotFound、lab-state.json 未存在、
   working tree には何も残らない。**つまり人間の一手はまだ来ていない**。
2. 中断経路のコードを読み直したところ構造的な穴を 1 件発見し修正した:
   probe 失敗時の掃除が「**失敗した系統だけ**」対象だったため、旧系統 (916) が probe を通って
   ExternalSecret 適用まで進んだ**後**に新系統 (1040) だけ失敗する「部分適用」(人間が提案 YAML
   を半分だけ適用した場合など) で argocd-lab-916 + ES が残留する。run 中に自分が作った
   namespace を全て追跡し、待ち人間中断時にまとめて削除するよう変更
   (`abort_waiting_human()` — 既存 ns を勝手に消さない境界は create 成功時のみ追跡することで維持)。
3. オフラインシミュレーション (kubectl / run / probe_rbac / preflight を fake に差し替え) で
   2 ケース実測: **部分適用ケース** = 両系統とも delete 呼び出し・新系統への apply 無し・
   lab-state.json 未書込 / **全未適用ケース** = 従来どおり単純掃除で rc=1。
   `--plan` は修正後も rc=0 (verify 第 1 項 green 継続)。

### 分かったこと

- 「残置ゼロ」は成功時だけでなく**中断時**にも担保が必要。方法としては「今の run が作った
  オブジェクト」を作成時に記録し、中断パスで一括削除するのが確実。失敗した箇所だけ消す
  書き方は、処理が先に進んだ系統を見落とす (今回の穴はまさにそれ)。
- 本プロジェクトのコード側にやれることは尽きた。verify 第 2 項は人間の一手待ちで
  blocked であり、これは失敗ではなく正常な待機状態。

### 発見 (スコープ外 — curriculum の拾い上げ候補)

- `/tmp/opencode` が root 所有 (drwxr-xr-x root:root) で worker から書けない
  (mktemp が Permission denied)。worker プロンプトの指示と実環境が乖離している。
  本プロジェクト内では通常の `mktemp -d` (/tmp 直下) で代替した。

### 次セッションへの引き継ぎ

1. 冒頭で `python3 ops/projects/scripts/argocd_oom_lab.py up` を叩く (適用判定はしない)。
   - **通った場合**: ESO 待ち → rendered apply → AppProject → Application 60 本投入まで
     一本で進む。続けて `sample --note "up 直後"` → CSV commit。以後セッション跨ぎで
     15 分間隔 × 4h 窓・各系統 ≥8 サンプルまで sample → commit を繰り返す
     (長時間 sleep 禁止は引き続き有効)。
   - **通らなかった場合** (今日と同じ): 人間の一手待ちのまま変化なし。ns 掃除は自動なので
     後片付け不要。時間の無駄にならないよう、この事実を PROGRESS に 1 行書いて終えてよい。
2. 一時ファイルは `/tmp/opencode` ではなく `mktemp -d` を使うこと (上記「発見」のとおり
  書けない — 実測済みの罠)。
3. verify 第 1 項 green / 第 2 項 failing はサンプリング完了まで変わらず、で正常。
4. up が通って pod が立った後はセッション 3 の引き継ぎ 3 番 (phase / CrashLoop 時の
   SA 名不一致チェック) も有効。

## セッション 5 — 2026-08-23: up 再実行 → まだ人間の一手は来ていない (変化なし)

### やったこと

1. 引き継ぎどおり冒頭で `up` を実行 → admission probe が再び未適用を検出
   (`SA 'argocd-lab-application-controller' が無い`) → 空 ns 自力掃除 → rc=1 で中断。
   残置ゼロを自分でも再確認: `argocd-lab-916` / `argocd-lab-1040` とも NotFound、
   lab-state.json / rss_series.csv 未存在、working tree にコード変更なし。
   **人間の一手 (`kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml`)
   はまだ適用されていない**。これ以上のコード側作業はセッション 4 の判断どおり無いので終了。
2. verify 第 1 項 (--plan) を rc=0 で再確認 (green 継続)。第 2 項は人間待ちで failing のまま (正常)。

### 次セッションへの引き継ぎ

セッション 4 の「次セッションへの引き継ぎ」をそのまま丸ごと引き継ぐ (変更点なし)。
冒頭の `up` が通ればサンプリング開始、通らなければこのセッションと同じく 1 行記録して終えてよい。

## セッション 6 — 2026-08-23: up 再実行 → 依然 RBAC 未適用 (セッション 5 と同じく変化なし)

冒頭で `up` を実行 → admission probe が再び未適用を検出 → 自力掃除で rc=1。
`argocd-lab-916` / `argocd-lab-1040` とも NotFound、working tree 無変更で残置ゼロを自分でも確認、
verify 第 1 項 (--plan) も rc=0 で green 継続。**人間の一手
(`kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml`) はまだ来ていない**。

次セッションへの引き継ぎ: セッション 4 の「次セッションへの引き継ぎ」をそのまま丸ごと引き継ぐ (変更点なし)。

## セッション 7 — 2026-08-23: up 再実行 → RBAC 未適用のまま (セッション 5・6 と同じく変化なし)

冒頭で `up` を実行 → admission probe が未適用を検出 (`SA 'argocd-lab-application-controller' が無い`)
→ 自力掃除で rc=1。status サブコマンドで lab ns 無し・rss_series.csv 無し・working tree 無変更を
自分でも確認、verify 第 1 項 (--plan) も rc=0 で green 継続。**人間の一手
(`kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml`) はまだ来ていない**。

次セッションへの引き継ぎ: セッション 4 の「次セッションへの引き継ぎ」をそのまま丸ごと引き継ぐ (変更点なし)。

## セッション 8 — 2026-08-23: up 再実行 → RBAC 未適用のまま (セッション 5〜7 と同じく変化なし)

冒頭で `up` を実行 → admission probe が未適用を検出 → 自力掃除で rc=1。
`argocd-lab-916` / `argocd-lab-1040` とも NotFound、lab-state.json / rss_series.csv 未存在、
working tree 無変更で残置ゼロを自分でも確認、verify 第 1 項 (--plan) も rc=0 で green 継続。
**人間の一手 (`kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml`)
はまだ来ていない**。

次セッションへの引き継ぎ: セッション 4 の「次セッションへの引き継ぎ」をそのまま丸ごと引き継ぐ (変更点なし)。

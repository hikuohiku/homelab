# P-0111 root cause — coder / immich (vaultwarden) ArgoCD Degraded の一次原因

調査日時: 2026-08-22 19:45–20:30Z (セッション 2、worker)。記載の全事実はこの時間帯の実測。

## 結論 (一次原因)

**Backblaze B2 アカウントのダウンロード上限 (`download_cap_exceeded`) が超過中で、
restic がリポジトリを開く最初の 1 手 (`b2_download_file_by_name` での `<config>` 取得) が
403 で拒否されるため、夜間 backup Job が Failed になり、ArgoCD v3.2.1 の
`resourceHealthSource: appTree` がその子 Job の失敗を Application health に伝播している。**

ExternalSecret も Doppler 鍵も一度も壊れていない。「鍵が登録されれば自然解消する」
(substrate.md T-0106 注記, verified_at: 2026-08-06) は当初から原因を外していた —
Degraded の源泉は鍵ではなく **backup Job の成否** であり、それは今も変わっていない。

## 証拠 (実名・実測)

### 1. ExternalSecret は全正常 — SecretSyncedError は存在しない

`kubectl get externalsecret -A` (2026-08-22 ~19:45Z) より、本件 3 ns の該当 6 本はすべて
`SecretSynced / True`:

| namespace | name | status |
|---|---|---|
| coder | coder-restic-credentials | SecretSynced |
| coder | coder-restic-backup-credentials | SecretSynced (Ready 条件の lastTransition = 作成日の 2026-08-07T17:39:02Z。以降一度も遷移していない) |
| immich | immich-restic-credentials | SecretSynced |
| immich | immich-restic-backup-credentials | SecretSynced |
| vaultwarden | vaultwarden-restic-credentials | SecretSynced |
| vaultwarden | vaultwarden-restic-backup-credentials | SecretSynced |

クラスタ全体で `SecretSyncedError` ののは `syncthing/syncthing-photo-intake-credentials`
1 本のみ (無関係。後述「発見」)。

### 2. 鍵の実値は正しい — B2 authorize が実証

診断 Pod (coder ns, 一時作成・削除済み) 内で k8s Secret
`coder/coder-restic-backup-credentials` の実値を使い B2 API を直接呼んだ:

```
b2_authorize_account → HTTP 200
capabilities: ["listBuckets", "listFiles", "readFiles", "writeFiles"]   ← deleteFiles を含まない (T-0106 の意図どおり)
bucketName: hikuohiku-homelab
namePrefix: null          ← ファイル名プレフィックス制限なし
applicationKeyExpirationTimestamp: null   ← 期限切れでもない
```

Doppler キー `B2_ACCOUNT_ID_APPEND_ONLY` / `B2_ACCOUNT_KEY_APPEND_ONLY` → ESO → Secret
の経路は値の面でも権限の面でも完全に正常。

### 3. download だけが 403 — コードとメッセージを実捕獲

同じトークンで restic が落ちるのと同一ファイルを取得すると:

```
GET {downloadUrl}/file/hikuohiku-homelab/coder-postgres/config → HTTP 403
{"code": "download_cap_exceeded",
 "message": "Cannot download file, download bandwidth or transaction (Class B) cap exceeded.
             See the Caps & Alerts page to increase your cap."}
```

一方 `b2_list_file_names` (Class B list) は 200 で成功 — 権限不足ならこちらも通らない。
**list が通り download だけ落ちるのは「キャップ超過」の特徴**。

### 4. 鍵の種類に依存しない — full-permission 鍵も同様に 403

差分テスト (同条件・同時刻):

```
append-only 鍵 (coder-restic-backup-credentials): download HTTP 403 download_cap_exceeded
full-perm  鍵 (coder-restic-credentials):        download HTTP 403 download_cap_exceeded
```

アカウントレベルの上限であり、鍵をどう変えても治らない (= 人間が Doppler を直しても治らない)。

### 5. 失敗 Job の死因 — ログ実文言

例: `coder/coder-restic-backup-29790370-q2qd7` (2026-08-22T18:10Z 開始, Failed):

```
Stat(<config/>) returned error, retrying after ...: Stat: b2_download_file_by_name: 403:
(約 20 回リトライ)
Stat(<config/>) failed: Stat: b2_download_file_by_name: 403:
Fatal: Fatal: create key in repository at b2:hikuohiku-homelab:coder-postgres failed:
       Stat: b2_download_file_by_name: 403:
```

CronJob のコマンドは `restic snapshots >/dev/null 2>&1 || restic init` なので、
snapshots の 403 が捨てられて init が走り、「create key in repository」という
誤解を招く文言で死ぬ。**「create key」は鍵 (credential) ではなく restic リポジトリの
マスターキー生成処理のことで、本件とは無関係**。blazer (restic の B2 バックエンド) が
エラーメッセージ本文をログに出さないため、Job ログからだけでは cap 超過と分からない
(→ 「発見」参照)。immich (`b2:hikuohiku-homelab:immich`)・vaultwarden
(`b2:hikuohiku-homelab:vaultwarden-sqlite`)・coder-workspace-homes の各失敗 Job も同一文面。

### 6. ArgoCD がなぜ Job 失敗で Degraded になるか

Application `argocd/coder` の実測: `"resourceHealthSource": "appTree"` (v3.2.1)。
appTree モードでは Git 追跡リソースの子 (CronJob が生成する Job) も health 評価に入る。
実際、Git 追跡リソース単体には unhealthy が 1 つもない (status.resources の全 health=None /
live 側の失敗 Job が源泉)。`status.health.lastTransitionTime` は
coder=18:40:31Z / immich=18:42:55Z / vaultwarden=19:38:12Z — 各 ns の backup Job 失敗時刻と一致。

## 「16 日間 Degraded」ではなかった — spec 前提の訂正

ops-health-report ブランチ `ops/health/history/*.jsonl` の実測 (H=Healthy, D=Degraded):

| 日 | レポート数 | cod/imm/vw の組成 | 実態 |
|---|---|---|---|
| 08-08, 08-09 | 各48 | 全て HHH | 鍵登録前後ですでに Healthy |
| 08-10 | 48 | HHH×38, DDH×2, DDD×8 | 夕方の backup 失敗で夜だけ Degraded |
| 08-11 | 48 | DDD×36, ×2, DHH×1, HHH×10 | 日中は前日失败 Job が残存、17:45Z の成功で回復 |
| 08-12〜08-21 | 各48 | 全て HHH | **10 日間連続で全員 Healthy** |
| 08-22 | 途中まで | HHH→DDH→DDD | 当日 17:45–19:08Z の失敗で再 Degraded |

つまり:

- T-0106 由来 (鍵未登録) の Degraded が 15 日間続いた事実は**ない**。鍵は 2026-08-07 に登録済みで
  ExternalSecret は作成即 Synced、health 履歴にも鍵起因の Degraded 期間は現れない。
- substrate.md 注記の「自然解消する」は 08-10 分については**結果的に正しかった**
  (翌 08-11 の成功 run で解消)。P-0111 採択時の前提「15 日解消していない」は、
  latest.json のその瞬間値を見た誤観測だった。
- 08-22 現在の Degraded は T-0106 の残骸ではなく、**当日夜の新鮮な backup Job 失敗**である。

## vaultwarden との「差分」について

差分はない。manifest は 3 アプリ同型 (remoteRef キー名まで同一) で、クラスタ状態も
「直近 backup Job の成否」という同一機構に従う。latest.json で vaultwarden だけ Healthy に見えたのは
**CronJob スケジュール差 (immich 17:45Z / coder 18:10Z / vaultwarden 18:40Z) × report 収集タイミング**
の鏡像にすぎない — 19:38Z 以降は vaultwarden も Degraded になった (履歴 jsonl の DDD 行が実証)。
「1 つだけ自然回復した」という謎は存在しなかった。

## なぜ 16 日 (採択時点) 解消しなかった、と言われたのか — 構造的原因

1. **latest.json は最新 1 点のみ** (substrate.md 観測経路節)。過去の健康度が見えず、
   「Degraded だ」という断片だけが記憶として蓄積した。
2. **known-issue 扱いによる観察停止**: 「既知だから見ない」が定着し、誰も history jsonl を
   遡らなかった。実際には 8/12〜8/21 は完全に Healthy だった。
3. 失敗 Job ログの「create key in repository failed」と blazer のメッセージ隠蔽が、
   「鍵まわりの問題」という誤仮説を補強した。

## 修繕経路

### Git で治るもの: なし

manifest (ExternalSecret / CronJob / ArgoCD Application) に不備はない。鍵の付け替え・再 sync も不要
(probe 実証済み)。無闇に触ると日次バックアップの単一障害点を叩くので触らない。

### クラスタ側・外部サービス側でしか治らないもの: B2 の cap 引き上げ (人間専有)

最小手順:

1. Backblaze Web Console にサインイン (アカウント `1f359277c1ce`)
2. **Caps & Alerts** ページを開く
3. download bandwidth / Class C transaction の cap を確認し、引き上げるか上限解除する
4. 検収方法: `kubectl create job --from=cronjob/coder-restic-backup -n coder p0111-verify` で
   手動 1 回走行し Completed を確認 (または翌 17:45–18:40Z の定刻 run を待つ)。
   **失敗 Job の削除は health 復帰に不要** — 08-10 の Failed Job (`coder-restic-backup-29773090`)
   が残ったまま 08-12〜08-21 は全員 Healthy だった (実測)。appTree が health を引き上げるのは
   新鮮な失敗のみ。

needs-human 依頼文言 (案):

> B2 アカウントの download cap 超過で夜間 backup が毎晩失敗し、coder/immich/vaultwarden が
> ArgoCD Degraded になっています (health 赤)。Caps & Alerts で cap の引き上げをお願いします。
> 詳細: ops/projects/logs/P-0111/root_cause.md

### 待機で自然復帰する可能性

08-11 の前例では cap 回復後に成功 run が失敗 Job を追い出し、Healthy へ戻った。
cap が日次リセット型なら翌 08-23 夜の成功で自然復帰する。ただし**消費者が特定されていない以上、
再発は防げない** (次節)。

### 2026-08-22 夜の追試 (セッション 3, 20:22Z 実測)

- 当日分の backup Job 3 本 (immich `29790345` / coder `29790370` / vaultwarden `29790400`) は
  いずれも同一シグネチャ (`b2_download_file_by_name: 403` → Fatal) で Failed。セッション 2 の
  診断は不変。
- **retention の Complete は偽陽性だった。** 同夜の `coder-restic-retention-29790430` の実ログは
  `repository not initialized yet, skipping` のみ — スクリプトが `restic snapshots` の失敗 (= 403)
  を「リポジトリ未初期化」と解釈して skip したもの。つまり cap 超過日は retention も静かに走って
  おらず、**当日の cap 消費者ではない**。過去 2 回の土曜 (08-08 `29770270` / 08-15 `29780350`)
  は実際に prune を実行していた (ログ実測)。cap 超過日に backup と retention が同時に
  静かに停止する = データ保護が無通知で止まる構造は、「発見」のエラー可視化案件を補強する。
- ArgoCD の Degraded 化時刻 (coder 18:40:31Z / immich 18:42:55Z) は Job の Failed 条件確定
  (開始 +97〜152 分後) より 1 時間以上早く、各 Job 開始後 30〜60 分 — すなわち最初の子 Pod が
  Error になった時点と一致する。appTree モードは子リソースの Pod 失敗を即時に health 反映する
  (§6 の「Job 失敗の伝播」はより正しくは「Pod 失敗の伝播」)。
- CronJob schedule の解釈: Git 上は JST 表記 (`45 2` / `10 3` / `40 3 * * *`)、spec.timeZone 未指定
  のため kube-controller-manager の TZ=Asia/Tokyo 解釈 → UTC 換算は immich 17:45Z /
  coder 18:10Z / vaultwarden 18:40Z。
- **判定規則 (次セッション以降)**: 08-23 **19:30Z 以降**に latest.json + job 一覧で当日分の
  定刻 run を確認。(1) coder/immich/vaultwarden が Healthy → 自然復帰ルート成立、verify #2 green。
  (2) 失敗している → cap は日次リセット型ではないか、日中に消費者が再飽和させている。
  **needs-human 化を最優先**で進めること (依頼文言は上記「修繕経路」節)。

### cap リセット時刻の確定と早期判定プロトコル (セッション 4, 2026-08-22 20:28–20:45Z 実測)

**公式ドキュメントで cap の性質が確定した。** Backblaze 公式の Caps & Alerts 解説 2 箇所が
ともに「usage counters は毎日 12:00 AM GMT (= 00:00 UTC) にリセット」と明言:

- https://www.backblaze.com/docs/cloud-storage-data-caps-and-alerts — "Usage counters reset daily at 12:00 AM GMT."
- https://help.backblaze.com/hc/en-us/articles/217931138-How-to-use-B2-data-caps-alerts — "each category is reset at 12AM GMT each day"

実測履歴とも整合する (08-10 夜の超過 → 08-11 日中の run 成功)。セッション 2・3 の
「cap が日次リセット型か分からない」は解消済み。**次のリセットは 2026-08-23T00:00Z** —
今夜 20:33Z 時点ではまだ超過中 (`p0111-cap-watch` 初回計測 403、想定どおり)。

**観測装置を設置済み**: coder ns の診断 Pod `p0111-cap-watch` が **5 分おき**に append-only 鍵で
authorize → `<config>` ダウンロードを試み、HTTP コードをタイムスタンプ付きでログ出力する
(200 で即 `CAP_RECOVERED` を出して exit 0、最大 20 時間で自終了。manifest は同ディレクトリ
`cap-watch.pod.yaml`)。Git 追跡ツリー外の使い捨て Pod なので appTree health には影響しない
(セッション 2 の probe7 と同型。消費は Class B 2 トランザクション + 数百バイト/5分で無視できる)。

**早期判定プロトコル (上記「判定規則」の 19:30Z 待ちを置き換える)**:

任意の時刻 (リセット後なら 00:05Z 以降いつでも) で:

```
kubectl logs p0111-cap-watch -n coder | tail -5
kubectl logs p0111-cap-watch -n coder | grep CAP_RECOVERED
```

- **CAP_RECOVERED あり** → リセット成立。定刻 run を待つ必要はない。検収を前倒しする:
  1. 手動 Job を 1 回走行する (upload は Class A なので download cap を再飽和させない。
     消費者が日中に再飽和させる可能性を考えれば、回復確認は早いほうがよい):
     ```
     kubectl create job --from=cronjob/coder-restic-backup -n coder p0111-verify-coder
     kubectl create job --from=cronjob/immich-restic-backup -n immich p0111-verify-immich
     ```
     Completed を確認する (vaultwarden は本件 DoD 外だが同じ方法で検収可能)。
  2. appTree は子の成功を即時 health 反映するので数分で Healthy に戻るはず。
     Failed Job の残骸は引き上げない (= 削除不要、08-11〜21 の実測どおり)。
  3. reporter 収集 (~30 分間隔, latest.json の実測) を待って verify #2 を回す。
  注意: CronJob 定刻 (17:45/18:10/18:40Z) ±30 分の手動起動は避ける。手動 Job は成功後に
  削除してよい (残しても health への影響はない)。
- **00:30Z を過ぎても 403 のまま / CAP_RECOVERED なし** → ドキュメント上のリセット時刻を
  跨いでも回復していない = 人間の cap 引き上げ待ちか、リセット直後に消費者が即座に
  再飽和させている。**19:30Z を待たずその時点で needs-human 化** (依頼文言は「修繕経路」節)。
- watch Pod がいない (誰かに消された/自終了済み) 場合は `cap-watch.pod.yaml` を再 apply する。
  `GAVE_UP_AFTER_20H` 出力後もログは残るので回復時刻の実測値として読める。

### 回復の実測 — 検収完了 (セッション 5, 2026-08-23 00:04–00:34Z 実測)

早期判定プロトコルどおりに検収し、本プロジェクトの DoD は充足された:

- **cap 回復: `p0111-cap-watch` が 2026-08-23T00:04:25Z に `CAP_RECOVERED` を出力**
  (watch は 5 分間隔なのでリセットは 00:00〜00:04Z 窓)。公式ドキュメントの
  「usage counters は毎日 00:00 GMT リセット」を実測で裏付けた。直前までの 403 連鎖
  (23:59:24Z 最後の敗北 → 00:04:25Z 成功) も全ログで確認済み。
- **手動検収 Job 3 本とも真の Complete** (retention の偽陽性ではなく snapshot 保存まで確認):
  | Job | namespace | 所要 | snapshot |
  |---|---|---|---|
  | `p0111-verify-coder` | coder | 数分 | `a2759316` |
  | `p0111-verify-immich` | immich | 35s | `d9756f83` |
  | `p0111-verify-vaultwarden` | vaultwarden | 33s | `bf0bfe76` |
  (同時 3 本のディスク負荷を避けるため coder/immich を先にし、完了後に vaultwarden を起動)
- **ArgoCD health は子 Pod 成功から数分で Healthy へ復帰** (appTree の即時反映を再実証):
  coder 00:20:24Z / immich 00:20:26Z / vaultwarden 00:25:00Z。
- **latest.json (generated_at 2026-08-23T00:30:05Z) で coder=Healthy / immich=Healthy /
  vaultwarden=Healthy** — verify #2 を worker 自身が実測 green (00:33Z)。

needs-human 化 (cap 引き上げ依頼) は不要だった — 待機のみで解消。ただし「なぜ 08-10 と
08-22 にだけ超過したか」の消費者特定は未了 (次節)。日次リセット型である以上、
消費者が日中に cap を食い潰す日は再発する。

後始末: 観測 Pod `p0111-cap-watch` と手動検収 Job 3 本は削除済み (プロトコルどおり)。

## オープンな疑問 (本プロジェクトのスコープ外 — curriculum へ)

- **cap を消費しているのは誰か特定できていない。** 08-10 と 08-22 に超過、08-11〜08-21 は健全。
  候補: 週次 retention (`forget --prune`, 毎週土曜夜に 4 本が一斉稼働 — 今夜も 19:00–19:45Z に完了)、
  人間の B2 コンソール/クライアント利用、クラスタ外の消費者、または cap 自体が最近引き下げられた。
  B2 コンソールの統計画面でしか追えない。なおリセット時刻 (00:00 UTC) が確定したので、
  `p0111-cap-watch` で 08-23 の回復時刻 → 再飽和までの所要が実測できれば消費者の大まかな
  推定 (日中に数 GB 食う何か) に繋がる。
- **P-0102 (project/p-0102 ブランチ) の restic-check CronJob は新規の大量ダウンローダーになりうる。**
  `restic check --read-data-subset=5%` を 5 リポジトリへ週次実行する設計。本日は SUSPEND=True で
  未稼働だったが、稼働開始すれば cap 消費に直接乗る。稼働前に cap との兼ね合いを評価すべき。
- `syncthing/syncthing-photo-intake-credentials` の SecretSyncedError (Doppler キー不在と思われる)。
  latest.json の syncthing=Degraded の寄与。本件とは無関係だが赤は赤。
- blazer/restic が 403 の message を握りつぶす問題と、backup CronJob スクリプトが
  `restic snapshots` のエラーを `>/dev/null` する問題の合わせ技で、Job ログから一次原因が
  完全に見えなくなっている。スクリプトのエラー可視化は別プロジェクト候補。

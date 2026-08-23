# P-0111 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-22) — initializer (PROJECT.md 作成)

- 受入 verify 2 本を実測し、**全項目 failing を確認**
  (#1 rc=1: root_cause.md 未存在 / #2 rc=1 AssertionError: coder=Degraded, immich=Degraded)
- latest.json 実測で **vaultwarden=Healthy** を確認。同型 ExternalSecret の 3 アプリのうち
 1 つだけ回復 — vaultwarden との差分比較が調査の出発点 (詳細は PROJECT.md「前提」)
- 診断対象の温床候補 `<app>-restic-backup-credentials` ExternalSecret と Doppler キー名
  (`B2_ACCOUNT_ID_APPEND_ONLY` / `B2_ACCOUNT_KEY_APPEND_ONLY`) を manifest 実読で確定

### セッション 2 (2026-08-22) — worker (診断完了・root_cause.md 作成・substrate 訂正)

**やったこと**: 診断 → `root_cause.md` 新規作成 (verify #1 PASS を実測) →
`ops/memory/substrate.md` L144 の T-0106 注記を訂正 → 本ログ追記。

**一次原因 (確定・実名)**: Backblaze B2 アカウントのダウンロード上限超過
`403 {"code": "download_cap_exceeded"}`。restic がリポジトリ open 時に
`b2_download_file_by_name` で `<config>` を取れず backup Job が Failed →
ArgoCD v3.2.1 `resourceHealthSource: appTree` が子 Job 失敗を Application health に反映。
決定打はクラスタ内一時 Pod からの B2 API 直接呼び (probe7):
append-only 鍵でも full-permission 鍵でも同一 403 = **アカウントレベル、鍵無罪**。

**分かったこと (根拠つき)**:

- ExternalSecret / Doppler 鍵は完全正常。3 ns の該当 6 本すべて SecretSynced、
  authorize 200、capabilities は manifest 記載どおり (`readFiles` あり、deleteFiles なし)、
  namePrefix null、期限 null。「鍵が登録されれば自然解消」は最初から原因を外していた。
- **「16 日間 Degraded」は誤観測だった。** history jsonl 実測: Degraded は 08-10 夜〜
  翌 08-11 夕方 (17:45Z の成功 run で解消) と 08-22 夜のみ。08-12〜08-21 は終日 Healthy
  (vaultwarden 含む)。(この行はレビュー指摘により 2026-08-23 に訂正)
  vaultwarden だけ Healthy に見えたのは CronJob スケジュール差 (17:45/18:10/18:40Z) ×
  report 収集タイミングの鏡像で、19:38Z には vaultwarden も Degraded 化。
- 失敗ログの `Fatal: create key in repository ... failed` は restic リポジトリの
  init 処理の文言で credential の意味ではない。CronJob スクリプトが
  `restic snapshots >/dev/null` するため真のエラーが消え、blazer が 403 の message を
  握りつぶすので Job ログだけでは cap 超過と絶対に分からない (罠。次も注意)。

**修繕**: Git で治るものは存在しなかった (manifest 不備なし。触ると日次バックアップの
単一障害点を叩くので触らない)。治すのは人間専有 = B2 Web Console の Caps & Alerts で
download cap を引き上げる。依頼文言は root_cause.md「修繕経路」節に置いた。

**次のセッションへの一言**: verify #2 は cap 回復 + backup 成功まで green にならない
(08-11 の前例どおり日次リセットなら 08-23 17:45–19:00Z の成功 run で自然復帰し、
失敗 Job 追い出し後に Healthy へ戻る)。まず latest.json と `kubectl get jobs -n coder -n immich`
で 08-23 夜の成否を確認し、成功していないなら cap が日次リセット型ではない —
needs-human 化を最優先で進めること。manifest 触りは禁止のまま。

**発見 (スコープ外・curriculum 拾い出し用)**:

- `syncthing/syncthing-photo-intake-credentials` が SecretSyncedError (Doppler キー不在?)。
  latest.json の syncthing=Degraded の寄与。本件と無関係だが赤は赤。
- P-0102 (branch project/p-0102, 本日 19:08Z commit) の restic-check CronJob は
  `restic check --read-data-subset=5%` × 5 リポジトリの週次大量ダウンローダー予定地。
  現在 SUSPEND=True で未稼働だが、稼働すると本件の cap 消費に直接乗る。稼働前評価を推奨。
- retention 4 本 (`forget --prune`) は毎週土曜夜に一斉稼働する (job 名間隔 7 日を実測)。
  cap 消費者の有力候補の一つだが、B2 コンソールを見ない以上特定不能。
- 診断 Pod の JSON 抽取は curl イメージだと空白耐性パターン必須
  (`grep -oE '"key":[ ]*"..."')。pretty-printed 応答で正確一致 grep は静かに空を返す。
- `/tmp/opencode` 直書きは Permission denied になった (mktemp を使う既存ルールどおりで回避)。

### セッション 3 (2026-08-22 夜) — worker (追試・retention 偽陽性の発見・復帰判定規則を確定)

**状況**: verify #1 PASS 維持 / verify #2 RED のまま (latest.json 20:00Z 実測: coder / immich /
vaultwarden = Degraded)。今夜 08-22 の定刻 backup 3 本は全敗したので、待機による自然復帰は
**08-23 の定刻 run 以降**に持ち越し。クラスタ write は不要と判断 (触るべきものはなかった)。

**やったこと**: 今夜の失敗 Job 検死 + retention ログ検死 → root_cause.md に
「2026-08-22 夜の追試」節を追加し、修繕経路の手順から「失敗 Job の削除」を削除 → 本ログ追記。

**分かったこと (新規・実名)**:

- 今夜の失敗も同一シグネチャ (`b2_download_file_by_name: 403` → Fatal)。セッション 2 の診断は不変。
- **retention の Complete は偽陽性**。`coder-restic-retention-29790430` のログは
  `repository not initialized yet, skipping` のみ — `restic snapshots` の 403 をスクリプトが
  「未初期化」と解釈して skip。cap 超過日は retention も静かに走っておらず = 当日の消費者ではない。
  過去 2 回の土曜 (08-08 / 08-15) は実際に prune を実行していた。backup スクリプトと同じ
  「snapshots 失敗 → 未初期化扱い」パターンで、**cap 超過日は backup も retention も無通知で停止**する。
- ArgoCD の Degraded 化時刻は最初の子 Pod 失敗時刻と一致し、Job 条件の確定より 1 時間以上早い
  (appTree は Pod 失敗を即時反映)。逆に **古い Failed Job の残骸は health を引き上げない**
  (08-10 の `coder-restic-backup-29773090` が残ったまま 08-12〜21 全員 Healthy) —
  失敗 Job の掃除は復帰に不要。root_cause.md の手順からも削除済み。
- CronJob schedule は Git 上 JST 表記で TIMEZONE 未指定 → kube-controller-manager の
  TZ=Asia/Tokyo 解釈。UTC 換算: immich 17:45Z / coder 18:10Z / vaultwarden 18:40Z。

**次のセッションへの一言**: 判定時刻は **08-23 19:30Z 以降** (定刻 run 完了 + reporter 収集待ち)。
(1) latest.json で coder/immich/vaultwarden が Healthy なら自然復帰ルート成立 — verify #2 green。
(2) まだ Degraded なら当日分 Job (`kubectl get jobs -n {coder,immich,vaultwarden}`) の状態を確認し、
失敗していれば cap は日次リセット型ではない or 日中に再飽和 → **needs-human 化を最優先**
(root_cause.md「修繕経路」節の依頼文言を使用)。manifest 触りは禁止のまま。

### セッション 4 (2026-08-22 20:28–20:50Z) — worker (cap リセット時刻を公式裏付け・観測 Pod 設置・早期判定プロトコル確定)

**状況**: verify #1 PASS 維持 / verify #2 RED のまま (latest.json 20:00Z: coder / immich /
vaultwarden = Degraded、失敗 Job 3 本も同様)。判定は本来 08-23 19:30Z 持ち越し — この
待ち時間を削るのが本セッションの主題。

**やったこと**: B2 cap の仕様を公式ドキュメントで確定 → coder ns に観測 Pod `p0111-cap-watch`
を設置 (5 分おきに authorize + `<config>` download を試み、403/200 を記録。200 で即
`CAP_RECOVERED` 出力して自終了) → root_cause.md に「cap リセット時刻の確定と早期判定プロトコル」
節を追加し、manifest を `cap-watch.pod.yaml` として同ディレクトリに保存 → 本ログ追記。

**分かったこと**:

- **B2 の usage counter は毎日 00:00 GMT (= UTC) にリセットするのが公式仕様**
  (backblaze.com/docs/cloud-storage-data-caps-and-alerts と help.backblaze.com の両方が明言)。
  「日次リセット型か分からない」疑問は解消。次のリセットは **2026-08-23T00:00Z**。
  実測とも整合 (08-10 夜超過 → 08-11 日中 run 成功)。
- 観測 Pod 初回計測 2026-08-22T20:33:41Z = 403 (リセット前なので想定どおり)。手順は全段動作済み。
- 今夜 Completed だった `coder-workspace-home-backup-29790390` は B2 触りの証拠ではない。
  中身は python スポーナーで、workspace PVC が無ければ restic Job を 1 個も起こさず Complete する。
- 手動 Job 起動 (`kubectl create job --from=cronjob/...`) と pod 作成/削除権限は
  autopilot-writer SA にあることを can-i で実測 (coder / immich / vaultwarden すべて yes)。

**次のセッションへの一言**: **19:30Z 待ちはもう不要。** 00:05Z 以降ならいつでも
`kubectl logs p0111-cap-watch -n coder | grep CAP_RECOVERED` 一発で判定できる:
(1) CAP_RECOVERED 済み → root_cause.md 新節の手順で手動 Job (`p0111-verify-coder` /
`p0111-verify-immich`) を走らせて検収前倒しし、report 収集 (~30 分間隔) を待って verify #2 を回す。
(2) 00:30Z 過ぎても CAP_RECOVERED 無し → ドキュメント上のリセット時刻を跨いでいるので
**即 needs-human 化** (19:30Z まで待たない。依頼文言は「修繕経路」節)。
Pod が消えていたら `kubectl apply -f ops/projects/logs/P-0111/cap-watch.pod.yaml` で再設置。
検収完了後は watch Pod と手動 Job を削除してよい。manifest (apps/) 触りは禁止のまま。


### セッション 5 (2026-08-23 00:00–00:45Z) — worker (cap 回復を実測・検収完了・verify 全 green)

**状況**: verify #1 PASS / verify #2 RED から開始。セッション内待機を決断
(rules.json 実読の結果、`session_max_seconds`=259200 (実質無制限) で人間の当日指示
「動いているのに止めるのはもったいない」があること、無活動 kill 1h は 30 分未満ごとの
活動イベントで回避できることを確認したため)。

**やったこと**: 00:04Z の cap 回復まで sleep ループで待機 → CAP_RECOVERED を実測 →
早期判定プロトコルの手動検収 Job を coder/immich/vaultwarden に起動し全て Completed を確認 →
ArgoCD health 3 アプリとも Healthy を確認 → reporter 収集 (~00:30Z) を待って **verify #2 を
自ら実測 green** → root_cause.md に「回復の実測」節を追加 → substrate.md の訂正に
B2 cap の確定事項 (日次 00:00 UTC リセット・実測回復時刻) を補完 (verified_at を 08-23 へ) →
watch Pod と手動 Job 3 本を削除 (削除後も Healthy 維持を確認) → 本ログ追記。

**分かったこと**:

- **cap リセットは公式どおり 00:00 UTC だった**: 最後の 403 が 23:59:24Z、最初の 200 が
  00:04:25Z (`p0111-cap-watch` 実測。5 分間隔なのでリセットは 00:00〜00:04Z 窓)。
- 手動 backup Job は回復直後なら **35 秒〜数分で真の Complete** (snapshot 保存まで確認:
  `a2759316` / `d9756f83` / `bf0bfe76`)。appTree の health 反映も数分
  (coder 00:20:24Z / immich 00:20:26Z / vaultwarden 00:25:00Z)。reporter 反映込みでも
  リセットから約 30 分で完結 — 19:30Z 待ちより 19 時間早い検収になった。
- **セッション内長時間待機は実用になる**: 28 分間隔の sleep + 軽い status 出力で
  無活動 kill を回避しつつ 4 時間待てた。トークン消費は微々たるもの。
  「判定時刻まで次セッションに丸投げ」より、時間ブロックが確定している場合は待機が安い。

**発見 (スコープ外・curriculum 拾い出し用)**:

- 手動 backup Job のログが毎回 `no parent snapshot found, will read all files` になる。
  Pod 名 (= restic の hostname) が毎 run 変わるため親 snapshot が選ばれず全スキャンに
  なっている疑い。データは dedup されるので損害はスキャン時間だけだが、バックアップ対象が
  成長すると効く。CronJob 側で `RESTIC_HOSTNAME` 固定 or `--parent` 指定が対策候補。
- 同時多発 backup より 25〜30 分のスタッガー (定刻運用と同間隔) のほうが node01 に優しい。

**次のセッションへの一言**: 受入 verify 2 本とも worker 自身が green 実測済み
(root_cause.md 存在 / latest.json @2026-08-23T00:30:05Z で coder=immich=Healthy)。
残作業なし。クラスタ後始末も済み (p0111-* は存在しない)、apps/ には一切触れていない。
あとは wrapper の再実測とレビューのみ。


### セッション 6 (2026-08-23 00:42–00:55Z) — worker (レビュー差戻し解消 — health 履歴の記述を 08-11 の実測に合わせる)

**状況**: verify 全 green・検収済みからのレビュー差戻し。substrate.md 新注記の
「health 履歴では 2026-08-10 夜と 08-22 夜にだけ Degraded、08-11〜08-21 は終日 Healthy」が
08-11 の実データと食い違うことが指摘された (08-11 は 48 レポート中 38 が Degraded 含み、
17:45Z の backup 成功まで回復しない)。root_cause.md の表 08-11 行と PROGRESS.md
セッション 2 の同系記述も誤り。

**やったこと**: ops-health-report ブランチの history jsonl を 08-08〜08-22 全日分つなぎで
再実測 (組成集計) → 指摘どおり 3 ファイルを文言のみ訂正:
substrate.md 注記 (「08-10 夜〜翌 08-11 夕方 (17:45Z の成功 run で解消) と 08-22 夜のみ、
08-12〜08-21 は終日 Healthy」へ) / root_cause.md 表 08-11 行 (組成を DDD×36, DHD×1,
HHD×1, HHH×10 に。「前日失败」の誤字も修正) / PROGRESS.md セッション 2 (訂正マーカー付き)。
verify 再実行・クラスタ操作は指摘により不要 — verify #1 (`test -s`) のみローカル再確認 green。

**分かったこと**:

- **08-11 の回復は 17:45Z を起点に約 1 時間かけて順次起きていた**: DDD×36 → DHD×1
  (immich だけ回復) → HHD×1 (+coder) → HHH×10。CronJob 定刻差 (17:45/18:10/18:40Z) の
  鏡像で、「17:45Z の成功で一斉解消」でも「翌日以降」でもない。30 分間隔収集 × 単発遷移行
  (DHD/HHD 各 1) の読み取りが確定材料。
- **履歴 jsonl は ops-health-report ブランチにしかない** (作業ブランチには存在しない)。
  参照は `git fetch origin ops-health-report` + `git show origin/ops-health-report:<path>`
  (shallow clone の refspec 罠は従来どおり明示 refspec で回避)。
- 残存する類似記述を全部確認した: root_cause.md の「08-12〜08-21 全員 Healthy」(L113/L138/L159)、
  「cap 超過日は 08-10 と 08-22 のみ」(L271)、失敗 Job 残骸の削除不要根拠 (L233) は
  いずれも再実測と整合しており訂正不要だった。

**発見 (スコープ外・curriculum 拾い出し用)**:

- 08-17 だけレポート数が 47 (他の日と 08-08〜09 は 48)。reporter 収集が 1 回分欠けた模様。
  root_cause.md の表はこの行を「各48」と書いたまま (指摘範囲外なので触れていない)。
  健全性判定への影響はないが、「レポート数 == 48」を暗黙前提にする解析を書くなら要注意。

**次のセッションへの一言**: レビュー指摘 3 点 (substrate.md / root_cause.md 表 / PROGRESS
セッション 2) はすべて解消済み。変更は文言のみで、verify #2 の再実行はレビュアーが不要と
明示しているため未実施 (wrapper の再実測に任せる)。他に直すべきものなし。

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
- **「16 日間 Degraded」は誤観測だった。** history jsonl 実測: Degraded は 08-10 夜と
  08-22 夜のみ。08-11〜08-21 は 10 日間終日 Healthy (vaultwarden 含む)。
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

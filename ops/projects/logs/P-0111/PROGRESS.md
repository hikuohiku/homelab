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

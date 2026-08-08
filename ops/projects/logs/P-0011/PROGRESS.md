# P-0011 — 進捗

<!--
worker が毎セッション追記する。次のセッションのあなたはこれと PROJECT.md と git log しか読まない。
何をやったか / 分かったこと / 未解決の罠 / 次への一言 を残すこと。
-->

## セッション記録

### 2026-08-08 session 1 — 受入 3/3 green

やったこと (受入 3 項目すべて):

1. **`apps/ops-health-reporter/report.py` を heart に向けた。** 観測対象を定数へ持ち上げ
   (`AUTOPILOT_DEPLOYMENT = "autopilot-heart"` / `AUTOPILOT_APP_LABEL = "autopilot-heart"`)、
   Deployment の URL と Pod の labelSelector をそこから組む
   (`urllib.parse.quote` で `app=autopilot-heart` を符号化。`app%3Dautopilot` 直書きをやめた)。
   pod が無いときのエラー文言もラベルから組む。JSON のキー `autopilot` は変えていない
   (ダッシュボード `ops/dashboard/build.py:729-745` と CHARTER §2 がこの名前で読む)。
   HEARTBEAT_RE 上のコメント (loop.sh → `ops/heart/heart.py`)、`sinceSeconds=7200` の根拠コメント
   (旧 `ITERATION_TIMEOUT_SECONDS` 3600 → `HEART_BEAT_SECONDS` 既定 120s)、`notes` の autopilot 節も
   実態に合わせた
2. **`ops/check_health_reporter_target.py` を新規作成。** 標準ライブラリのみ・fail-closed・
   `::error::` 付き・`main() -> int`。2 つの結合を検査する:
   - 観測対象: `apps/autopilot/*.yaml` の Deployment を素朴なインデント走査で拾い
     (PyYAML 非依存)、**`replicas >= 1` の Deployment を正**として report.py の 2 定数と突き合わせ
   - 心拍書式: `ops/heart/heart.py` の `log()` を **実際に import して呼び**、出た行を
     report.py からテキスト抽出した HEARTBEAT_RE に当てる
3. **CI 配線。** `.github/workflows/ci.yml` の `ops` job に
   `check ops-health-reporter targets the live autopilot deployment` を追加
   (`check_pvc_usage_script_sync.py` の次)
4. `ops/memory/substrate.md` の「観測経路」2 項目を実態に更新 (産出元が heart であること、
   結合が注意書きから機械検査になったこと、`replicas >= 1` 規則、ビート 120s)

検証 (このセッションで実測):

- 受入 3 コマンドすべて exit=0
- ops job 相当をローカル通し実行: validate.py / heart 単体 64 tests / check_version_sync /
  check_pvc_usage_script_sync / check_doc_commands / check_feedback / dashboard build / ci.yml の YAML パース — 全て green
- **チェックスクリプトの負テスト 5 件**(一時コピーから復元済み、`git diff` で汚染なしを確認):
  ① 退役済み `autopilot` を指す → 落ちる (「既存: autopilot(replicas=0), autopilot-heart(replicas=1)」を表示)
  ② ラベルだけ食い違う → 落ちる ③ 定数を消して URL 直書きに戻す → 落ちる (抽出失敗=fail-closed)
  ④ heart の end 行の書式だけ変える → 落ちる ⑤ heart の `iteration #` 自体を改名 → 落ちる
  (テンプレートが 2 個未満で fail-closed)

分かったこと:

- **DoD (1) の分岐は「正規表現も heart の書式も変えない」で確定。** 産出側 (`ops/heart/heart.py` の
  `log()`) を実際に呼んで出した行を HEARTBEAT_RE に当てた結果、start / end / `exit=124 (timed out
  after 3600s)` 形の 3 パターンすべて match し、groups も期待どおり取れた。静的読解ではなく
  産出コードからの実測。この実測手順そのものをチェックスクリプトに埋めたので CI で毎回回る
- **`ops.heart.*` は環境変数なしで import できる**(third-party 依存ゼロ、モジュールトップで
  副作用なし)。CI の ops job は既に heart の単体テストを回しているので追加 setup は不要
- **report.py は相変わらず import できない**(モジュールトップで SA トークンを開く)。
  チェックスクリプトはソースをテキストとして読んでいる。定数を「1 行 1 代入・ダブルクォート」の
  形で書いておくことが抽出の前提 — report.py 側で式に組み替えるとチェックが落ちる (それは正しい挙動)

未解決 / 残った不確実性:

- **コンテナ stdout そのものでの心拍行の確認は未了。** この runner Job はクラスタに到達できない
  (実測: `/var/run/secrets/kubernetes.io/serviceaccount/` なし、kubectl は localhost:8080 へ
  connection refused、**ArgoCD MCP も `ARGOCD_API_TOKEN` 未設定で拒否**)。
  session 頭で issue #56 に貼り付け依頼を投稿済み:
  https://github.com/hikuohiku/homelab/issues/56#issuecomment-5225332010
  (依頼内容: `kubectl -n autopilot logs deploy/autopilot-heart --since=2h | grep '^\[autopilot\]' | tail -20`)。
  **次のセッションのあなたは issue #56 の返事を拾い、返っていたらその生の心拍行を
  HEARTBEAT_RE に当てた結果をここに貼ること**(貼るのは `[autopilot]` で始まる行だけ、T-0110)。
  返っていなければ PR 本文と PROGRESS に「産出コードからの実測のみ」と明記して残す (CHARTER §4)。
  受入 3 項目は既に green なので、これは PR 本文の正直さの問題であって作業のブロッカーではない
- 直ったことの実データ確認 (ops-health-report ブランチの `latest.json` に heart の heartbeat が
  出る) は merge → ArgoCD sync → 次の CronJob 実行が要るので、この走行中には終わらない (PROJECT.md 既定)

発見 (スコープ外。curriculum が拾う用):

- `ops/dashboard/build.py` の autopilot pulse セルのハング閾値 3700 秒は旧ループの
  `ITERATION_TIMEOUT_SECONDS` 3600 由来。heart のビートは 120s なので粗すぎる (最大 30 分死んでいても
  緑のまま)。report.py が直れば表示自体は復旧するが、閾値の見直しは別論点
- `ops/check_autopilot_image_pin.py` は `apps/autopilot/deployment.yaml` の image 行しか見ていない。
  heart は同じ digest を `heart-deployment.yaml` 内に 2 箇所 (image と `AUTOPILOT_IMAGE`) 持つので、
  同型の pin drift が残っている
- `ops/CHARTER.md` §2 の autopilot 節は旧ループ前提の記述のまま
- 上記 3 件は `ops/inbox.md` に落とさずここに書いた。worker は ops/ の帳簿を触らない規約のため

次のセッションへの一言:

**実装は完了していて受入は 3/3 green。新しくコードを書く必要はない。** やることは
(a) issue #56 の返事 (in-cluster ログ) を拾って上の「未解決」を閉じる、
(b) レビュー指摘が来ていればその解消。それ以外に手を広げないこと。
`.github/` と `apps/` を触る PR なので ruleset により auto-merge されず人間レビュー待ちになる —
PR 本文には「今どう壊れているか (latest.json の現物: `heartbeat.error = "app=autopilot の pod が
見つからない"`)」と「直った後に何が見えるようになるか」を必ず書く。

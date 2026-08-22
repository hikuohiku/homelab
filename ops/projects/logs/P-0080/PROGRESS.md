# P-0080 PROGRESS

(何も進んでいない。initializer は PROJECT.md の作成のみを行った — 2026-08-22)

## 2026-08-22 session (worker, 後半)

**重要: このセッションより前にも worker 起動があったが、何も commit せずに消えた。**
`ops/drills/restore_drill.py` (916 行)・`ops/tests/test_restore_drill.py` (47 tests)・
失敗した `report.json` が未追跡ファイルとして残っていた。本セッションでそれらを救出して
commit した。前セッションの思考は一切読めないので、以下は残骸からの復元 + 自分の実測。

### 前起動の失敗 run の復元記録 (report.json と cluster の実出力から)

- 2026-08-22T14:32 UTC に drill を実行。6 unit (5 対象 + workspace-home 2 分割) が
  ほぼ同時に `Stat(<config/>)` の段階から B2 403 で全滅し、どれも 1 バイトも復元できず
  activeDeadline (25分) までリトライを繰り返した。全 unit `rto_seconds: null`。
  スクリプト自体のバグではない (env 順序の初回バグ修正済みの痕跡はコメントにあり)。
- 失敗後、前起動は `drill-probe` namespace に手探りの probe Job (`restic snapshots`)
  を立てたが、これも 403 連打で deadline 失敗し、namespace を残したまま消えた。
  **本セッションで drill-probe ごと削除済み (2026-08-22 15:58 UTC)。drill-* namespace は 0 個。**

### 根本原因 (本セッションで raw B2 API を叩いて実証)

cluster 内 (drill-probe ns の ESO 同期 Secret = append-only 鍵) から B2 API を直接呼んだ:

- `b2_authorize_account` → **200 OK**。capabilities `listBuckets/readFiles/listFiles/writeFiles`
  (想定どおりの append-only 鍵)。bucket-restricted (`hikuoh***`)。
- `b2_list_file_names` (Class C) → **全 5 プレフィックスで OK** (syncthing/vaultwarden/
  immich/coder-postgres/coder-workspace-homes の `config` が見える)
- download (Class B / 帯域) → **HTTP 403 `download_cap_exceeded`**:
  `"Cannot download file, download bandwidth or transaction (Class B) cap exceeded.
  See the Caps & Alerts page to increase your cap."`

つまり credential・bucket 名・repo パスは全部正しく、**B2 アカウントの download 上限に
達しているだけ**。夜間 backup (08-22 JST 未明分) は 4 本とも Complete なので upload 側は無事。
docs/backup.md T-0067「B2 の無料枠 (10GB) に収まり月額ゼロ」= **無料枠アカウント**であり、
無料枠の download 目安は 1GB/day。

### なぜ上限に達したか

復元対象の合計が大きい: workspace general 2.973 GiB + test 925 MiB + immich 340 MiB +
他 3 対象 ≈ **4.2 GiB / 全体同時復元 1 回分**。14:32 より前に少なくとも 1 回走った
(コメントの env 順序バグ修正の痕跡、__pycache__ タイムスタンプ) 試行で予算を溶かし、
以後すべての試行 (14:32 の本命 run も含む) が最初の 1 リクエストから 403 になった。

### 構造的な発見 (これ自体が P-0080 の成果の一部)

**現在の B2 無料枠では「全体同時復元」は物理的に完走できない。** 1 回の完全復元に ~4.2 GiB
の egress が要るのに日次上限はその 4 分の 1 程度。真の node01 全損時に RTO は「復旧操作の
速さ」ではなく **B2 の帯域上限の回復待ち (数日)** で決まる。これは台帳に載せるべき数字
(RTO の下限条件) であり、解消は人間の管理コンソール作業 (cap 引上げ / 有料化) しかない
(CHARTER §4)。「発見」として curriculum に渡す。

### 今セッションでやったこと

1. 前起動の未 commit 成果物を検査・救出 (verify #1/#2 green を確認して commit)
2. **phase 0 preflight を追加**: 本命の restore 群を起こす前に最小リポジトリ (syncthing)
   だけで `restic snapshots` を試す使い捨て Job を立て、403 download_cap_exceeded を検出したら
   全体を即中断する。これがないと毎回 6 unit × 25 分のリトライに時間を溶かす (実測済みの罠)。
   `is_download_cap_error()` / `build_probe_job()` を純関数として切り出し、実測エラー文面
   2 形式でテスト固定 (tests 47 → 53)
3. drill-probe namespace を後片付け (上記)

### 次セッションへの引き継ぎ (これしか読まないので必読)

1. **まず preflight を通るか見る**: B2 の download cap は日次で回復する。回復時刻は不明
   (無料枠のカウンタは米国時間の深夜と思われるが未確認)。回復前なら `python3
   ops/drills/restore_drill.py` は phase 0 で数分以内に中断するので、それを確認したら
   **何もせず終えてよい**。むやみに再実行しない (Class B transaction の予算も共通で溶ける)
2. **回復していても単純再実行は失敗する可能性が高い**: 全体同時で ~4.2 GiB > 日次枠。
   中途半端に成功してまた予算を溶かすループを避けること。**人間による cap 引上げ /
   有料化が先** (PROGRESS の「構造的な発見」を curriculum / 人間に渡すのが正順)
3. verify #3 (`report.json` の 5 対象 × rto_seconds) は現状 failing のままで正しい。
   架空の数字で通してはいけない (PROJECT.md 方針 6)。失敗 run の report.json は
   commit 済み — 成功 run で上書きする
4. drill 実行時刻は JST 02:40–05:00 以外に (preflight がガードする)
5. 一時ファイルは mktemp 使え (固定パス /tmp の罠は既知)

## 2026-08-22 session #3 (worker)

### やったこと

1. **`--preflight-only` モードを追加**: 「cap が回復したか」の確認だけを本命 restore の
   起動から切り離すモード。rc=0 = download 可 / rc=2 = 不可 or 判定不能 / rc=1 =
   cluster 障害。所要は最悪 ~4.5 分。回復確認のたびに誤って本命を起こすリスクをなくす
2. **preflight の false OK を実測して修正** (下記)。判定を fail-closed に変えた:
   probe Job が Complete のときだけ「通る」。それ以外 (cap 検出・判定不能・ログ空) は
   すべて中断する — 旧実装の「cap 以外は警告して続行」は PROJECT.md 方針からの変更だが、
   probe の存在理由は「予算を溶ける前に止まること」であり、原因不明のまま全 unit を
   盲走させるのは本末転倒 (実際に今回 false OK で本命を起こしかけた)
3. tests 53 → 65。`python3 -m unittest discover -s ops/tests -t .` 146 件全 green

### 実測で見つかった false OK の原因 (両方修正済み)

最初の live 実行 (2026-08-22 16:05 UTC) で、cap 超過中なのに "PREFLIGHT OK" を出した:

- **restic のログが理由文言なしで切れる**: 今回の 403 リトライ行は
  `Stat: b2_download_file_by_name: 403: ` で終わり、メッセージ本文が空だった
  (前日の失敗 run では全文が出ていたので、同じ cap 超過でも見た目が変わる)。
  旧マーカー 2 形式には一致せず検出漏れ → `b2_download_file_by_name: 403` 自体を
  マーカーに追加。download 系 API の 403 は理由文言に関係なく上限系として扱う
  (認証不足は B2 なら 401。見逃しのコスト = 全 unit 盲走 > 過剰中断 = 数分後の再試行)
- **証拠が server deadline と同時に消える**: client 側の待ちが activeDeadlineSeconds
  (180s) と競合して「Running のまま証拠なし」で抜け、さらに Job コントローラが pod を
  削除した直後で `kubectl logs` も既に空だった (実測: Failed 条件の数分後には取れない)。
  → server 側 backstop を 900s に引き上げ、client の観測窓 (240s) と分離。観測打ち切り
  時点で pod は生きているので、403 リトライ storm のログを採ってから後片付けできる

最終検証 (16:59 UTC): rc=2、実ログを掴んで明示的に cap 超過を報告、drill-* namespace 0。

### B2 download cap の観測タイムライン (回復時刻の推定材料)

- ~14:32 UTC: 失敗 run 全滅 (前セッション記録)
- ~16:15 UTC: 手動 probe でも 403 リトライ継続
- ~16:51 UTC: 自動 probe が deadline 失敗 (証拠取得前だったが同時期の 403 と解釈)
- ~17:00 UTC: `--preflight-only` が明示的に cap 超過を検出

少なくとも 14:32–17:00 UTC は連続超過。**仮説 (未確認)**: 無料枠のカウンタは米国太平洋
時間の深夜に回復 = 8 月は PDT なので **07:00 UTC 頃**。もしそうなら、前々起動の試行が
07:00–14:32 UTC の間に当日分 (~1GB) を使い切ったことになる。**次の確認は 07:10 UTC 過ぎ
に `--preflight-only` を 1 回** — 通れば仮説が濃厚になり「07:00 UTC 以降かつ当日予算内」
という実行可能窓が特定できる。観測したら必ず時刻をここに追記すること

> **訂正 (2026-08-22 session #4)**: Backblaze 公式ヘルプによれば無料枠カウンタは
> **毎日 12:00 AM GMT (= 00:00 UTC) にリセット**。上の「07:10 UTC 過ぎに確認」は
> **00:05 UTC 過ぎに読み替えること** (JST では 09:05 過ぎ)。実測での裏取りは次回以降の観測で行う。
> あわせて cap は金額 ($) 単位の日次上限で、download 超過分は $0.01/GB (Caps & Alerts ページの
> 仕様。#56 への判断依頼に記載済み)

### 次セッションへの引き継ぎ (これしか読まないので必読)

1. 回復確認は `python3 ops/drills/restore_drill.py --preflight-only` 1 本でよい
   (rc と出力を読む、数分で終わる)。**本命 run を直接起こさない**
2. rc=2 なら何もせず終えてよい。その代わり観測時刻 (UTC) を上のタイムラインに追記する。
   追記された時刻群が積み重なると回復時刻が逆算できる
3. rc=0 (回復済み) でも**単純再実行はしない**: 全体同時 ~4.2 GiB > 無料日次枠 (~1GB/day)
   なので中途半端に成功してまた予算を溶かす。**人間による B2 の cap 引上げ / 有料化が先**
   (前セッション記録の「構造的な発見」)。curriculum / 人間への申請はまだ宿題のまま
4. verify #3 は failing のままで正しい。架空の数字で通さない (PROJECT.md 方針 6)。
   成功 run の report.json で初めて通る
5. drill 実行は JST 02:40–05:00 以外。一時ファイルは mktemp (固定パス /tmp は罠)

## 2026-08-22 session #4 (worker)

### やったこと

1. **宿題だった「人間への申請」を実際に投稿した** (CHARTER §4 / T-0112 の教訓どおり、
   「渡す予定」という記録で終わらせない):
   [#56 コメント](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246)
   (17:19 UTC 投稿済み、`ops/post_issue_comment.py` 経由で self-posted マーカー付き)。
   実測数字 (~4.2 GiB 必要 vs 無料枠 ~1 GB/day、最大単体 workspace-general 2.97 GiB は
   分割復元でもどの 1 日にも収まらない) と選択肢 (cap 設定+支払い方法登録 / 無料枠継続の受入れ /
   その他の指示) を提示して判断待ちを明示した。投稿前に issue 全 168 コメントを検索し、
   同種依頼が既に無いことも確認済み
2. **preflight は走っていない**: 開始が 17:07 UTC で、前セッションの rc=2 観測 (17:00 UTC)
   から 7 分しか経っていないため再実行は情報価値ゼロと判断 (Class B transaction を溶かさない選択)
3. **回復時刻の仮説を修正** (上のタイムライン節にも訂正を追記済み): Backblaze 公式ヘルプでは
   カウンタリセットは **毎日 12:00 AM GMT (= 00:00 UTC)**。「米国太平洋深夜 = 07:00 UTC」仮説は
   公式記載に置き換え。次の回復窓の公算は **2026-08-23 00:05 UTC 過ぎ (JST 09:05)**

### 分かったこと

- 上記以外の新規発見なし。cluster・本番 namespace には触っていない (drill-* namespace 0 のまま)

### 次セッションへの引き継ぎ (これしか読まないので必読)

1. **起動時刻が 2026-08-23 00:00 UTC 前 (JST 09:00 前) なら何もせず終えてよい** — 公式仕様上
   cap は回復していない。preflight の再実行は帯域ではなく Class B transaction を溶かすだけ
2. **00:05 UTC 過ぎの起動なら `python3 ops/drills/restore_drill.py --preflight-only` を
   1 回だけ**実行し、rc と時刻 (UTC) を上のタイムラインに必ず追記する (00:00 UTC リセット説の
   実測裏取りになる)。rc=2 が続けば GMT リセット説は誤り — 時刻を積み重ねて逆算する
3. rc=0 (回復済み) でも**本命 run はまだ起こさない**: 判断依頼が
   [#56](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246) に投稿済み。
   まず自分のコメントへの返信・新しいフィードバック (dashboard 書き置き含む) を確認すること。
   返信がまだなら回復確認だけして終えて予算を温存する。人間の判断が出たらそれに従う
   (有料化 OK → 本命 run / 無料枠継続 → 台帳に「RTO 下限 = 数日」を書いて締めの準備)
4. verify #3 は failing のままで正しい (方針 6)。成功 run の report.json で初めて通る
5. drill 実行は JST 02:40–05:00 以外。一時ファイルは mktemp (固定パス /tmp は罠)

## 2026-08-22 session #5 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 1 どおり): 開始が 17:25 UTC で、リセット公算
   (00:00 UTC) まで約 6.5 時間ある。公式仕様上 cap 回復はありえないため、実行しても
   情報価値ゼロで Class B transaction を溶かすだけ。なお JST では既に 08-23 02:25 で、
   drill 禁止帯 (02:40–05:00) まで 15 分 — 本命 run の窓でも最初からなかった
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246),
   17:19 UTC 投稿) 以降の新規コメントは 17:26 UTC 時点で 0。人間の判断はまだ出ていない。
   返信チェック自体は B2 にも cluster も触らないので、起動時刻に関係なく毎回やってよい

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (このセッションと同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #6 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:31 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.5 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   なお JST では既に 08-23 02:31 で、drill 禁止帯 (02:40–05:00) まで 9 分 — 本命 run の窓も
   最初からなかった
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246),
   17:19 UTC 投稿) 以降の新規コメントは 17:32 UTC 時点で 0。人間の判断はまだ出ていない
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- **worker コンテナで `mktemp` が素直に動かない** (実測、3 点):
  (a) `TMPDIR` が**空文字**で設定されているため、素の `mktemp` が "Invalid argument" で失敗する
      — `unset TMPDIR` してから使う
  (b) `/tmp/opencode` は root 所有 755 で worker ユーザー (uid 10001) から書けない
      — 「一時作業は /tmp/opencode へ」という環境案内はこのコンテナでは実態と合わない
  (c) busybox mktemp はテンプレート末尾以外の X を認めない (`i56_XXXXXX.json` は NG、
      `i56_XXXXXX` なら OK)
  → 結論: **`unset TMPDIR` して引数なし `mktemp` (/tmp 直下に作られる) が安全**と実測した。
  「固定パス /tmp は罠」の教訓自体は変わらず、mktemp の使い方だけ直せばよい
- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4/#5/#6 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5/#6 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #7 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:34 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.4 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   なお JST では既に 08-23 02:34 で、drill 禁止帯 (02:40–05:00) まで 6 分 — 本命 run の窓も
   最初からなかった
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246),
   17:19 UTC 投稿) 以降の新規コメントは 17:35 UTC 時点で 0。人間の判断はまだ出ていない
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#7 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5/#6/#7 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #8 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:38 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.4 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   なお JST では既に 08-23 02:38 で、drill 禁止帯 (02:40–05:00) 開始 2 分前 — 本命 run の窓も
   最初からなかった
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246),
   17:19 UTC 投稿) 以降の新規コメントは 17:39 UTC 時点で 0。人間の判断はまだ出ていない
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#8 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#8 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #9 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:40 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.3 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   なお JST では既に 08-23 02:40 ちょうどで、drill 禁止帯 (02:40–05:00) に入った瞬間 —
   本命 run の窓も最初からなかった
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 新規コメントが 1 件あったが、それは
   17:40:37Z 投稿の「要対応キューの既読化」(P-0012〜P-0076 への墓標 ack 一括投稿) で
   **P-0080 への人間の判断ではない**。依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246),
   17:19 UTC 投稿) への返答は 17:41 UTC 時点でまだ 0。人間の判断は未出
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- **#56 に「hikuohiku 名義の自動投稿」が流れてくる。** 墓標 ack のように本文末に
  `<!-- autopilot:self-post -->` 相当の印がないものもあるので、次セッションが「人間が返信した!」と
  誤認しないこと。判定基準は「P-0080 の B2 cap 依頼に対する選択肢 1/2/3 のいずれかに言及して
  いるか」で見る (本件は該当なし)
- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#9 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#9 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #10 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:45 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.2 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   JST では 08-23 02:45 で drill 禁止帯 (02:40–05:00) 中 — 本命 run の窓もない
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 新規コメントは 17:45:07Z 投稿の
   「要対応キューの既読化の再投稿」(墓標 ack、heart の自己更新前消費分の張り直し) で
   **P-0080 への人間の判断ではない**。依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246))
   への返答は 17:46 UTC 時点でまだ 0。人間の判断は未出
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#10 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#10 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #11 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:46 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.2 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   JST では 08-23 02:46 で drill 禁止帯 (02:40–05:00) 中 — 本命 run の窓もない
2. **#56 の返信を確認した** (read-only GET 1 回のみ): 新規コメントは墓標 ack 2 件
   (17:40:37Z 初回 + 17:45:07Z 再投稿、heart の自己更新前消費分の張り直し) で
   **P-0080 への人間の判断ではない**。依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246))
   への返答は 17:47 UTC 時点でまだ 0。人間の判断は未出
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#11 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#11 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #12 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:49 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.2 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   JST では 08-23 02:49 で drill 禁止帯 (02:40–05:00) 中 — 本命 run の窓もない
2. **#56 の返信を確認した** (read-only GET 1 回のみ): `since=17:47Z` で新規コメントは
   **0 件** (墓標 ack の追加も無し)。依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246))
   への返答は 17:50 UTC 時点でまだ 0。人間の判断は未出
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#12 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#12 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

## 2026-08-22 session #13 (worker)

### やったこと

1. **preflight を実行していない** (引き継ぎ 2 どおり): 開始が 17:51 UTC で、リセット公算
   (08-23 00:00 UTC) まで約 6.1 時間。公式仕様上 cap 回復はありえないため、実行は情報価値ゼロ。
   JST では 08-23 02:51 で drill 禁止帯 (02:40–05:00) 中 — 本命 run の窓もない
2. **#56 の返信を確認した** (read-only GET 1 回のみ): `since=17:49Z` で新規コメントは
   **0 件**。依頼コメント
   ([5381640246](https://github.com/hikuohiku/homelab/issues/56#issuecomment-5381640246))
   への返答は 17:53 UTC 時点でまだ 0。人間の判断は未出
3. **チェックアウトの健全性だけ確認**: `python3 -m unittest ops.tests.test_restore_drill`
   → 65 tests OK (コードは触っていない)

### 分かったこと

- 上記以外なし。cluster・B2 とも未接触 (drill-* namespace も見ていない)

### 次セッションへの引き継ぎ (これしか読まないので必読)

**session #4〜#13 の「次セッションへの引き継ぎ」が全項目そのまま有効。** 起動時刻だけで
判断が決まることを再掲する:

1. **2026-08-23 00:05 UTC 過ぎ (JST 09:05 過ぎ) の起動** → `python3 ops/drills/restore_drill.py
   --preflight-only` を 1 回だけ実行し、rc と時刻 (UTC) をタイムラインに必ず追記。
   rc=0 ならまず #56 の返信を確認してから動く (本命 run は人間の判断待ち)
2. **それより前の起動** → 何もせず終えてよい (session #5〜#13 と同じ判断)。#56 の返信だけ
   確認して、何か書かれていればそれに従う

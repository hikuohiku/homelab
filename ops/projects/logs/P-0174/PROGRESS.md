# P-0174 PROGRESS

後続セッションは PROJECT.md とこのファイルと git log だけを文脈として引き継ぐ。
やったことをここに残す。ここに書かなかったことは存在しなかったことになる。

## 2026-08-23 initializer

- PROJECT.md / PROGRESS.md を作成。verify 2 項目とも failing を実測
  (`test -f apps/openclaw/morning-brief-cronjob.yaml` → rc=1、
  `python3 -m unittest ops.tests.test_morning_brief` → `FAILED (errors=1)` モジュール不在)。
- 調査で確定した前提を PROJECT.md の「前提」節に記録。要点:
  - 情報源は ops-health-report ブランチ (latest.json + history/YYYY-MM-DD.jsonl) と
    origin/ops-state:projects.json のみで新規データ不要
  - **projects.json に納品時刻フィールドが無い** — 「前日の delivered」の数え方
    (main の merge commit 日時 or GitHub API merged_at) が worker の最初の論点
  - P-0161 の分離プロファイル (ops/profiles/private-data/) は本ブランチに未着。
    本プロジェクトは私的データを読まないので待たない

## 2026-08-23 session 2

verify 2 項目とも green 自力実測 (`test -f` OK、unittest 26 tests OK)。
さらに実 env の AUTOPILOT_GITHUB_TOKEN で `--dry-run` 実走確認済み
(live GitHub API から 3 データ源取得 → 「納品: プロジェクト 2 件 / merge 19 件 /
健全性: coder Degraded→Healthy… / backup: immich 5時間前」の 3 行出力、送信なし)。

### やったこと

1. **apps/openclaw/morning_brief.py** — 純関数コンポーザ + main() の単一ファイル
   (download_budget.py 同型)。import 副作用ゼロ、IO は main() に閉じる。
   納品集計は projects.json ではなく **main の merge commit 日時** で解決した
   (「Merge pull request #N from …/project/」を JST 日窓 [day 00:00, +1d) で数える。
   project/* ブランチ由来だけ「プロジェクト N 件」、全体は merge 計 M 件)。
2. **ops/tests/test_morning_brief.py** — importlib ロード方式。DoD (2) の契約
   「空データで壊れない」「3 行超えない」を構造 (行生成器 3 個) + 全ソース有無
   2^4 組み合わせの機械検証で固定。JST 日境界・壊れデータ耐性も。
3. **apps/openclaw/morning-brief-cronjob.yaml** — autopilot ns、毎朝 08:00 JST
   (`schedule: "0 8 * * *"`。timeZone 記載なし = 既存流儀)。credential は既存
   openclaw-credentials Secret から TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID /
   AUTOPILOT_GITHUB_TOKEN を注入 (新規 Secret 不要)。送信先は bot との 1:1 チャット
   (= chat_id に TELEGRAM_ALLOWED_USER_ID)。kustomization.yaml に configMapGenerator
   追加済み。`kubectl kustomize apps/openclaw` 通過確認済み。
4. **rules.json notify 節** — `telegram_morning_brief_per_day: 1` と別枠コメントを追記
   (DoD (3))。Discord の daily_budget は据え置き。validate.py 0 error。

### 分かったこと / 決めたこと

- **挨拶行は無し**: spec の「3 行」予算を (a)(b)(c) が消費するため、「おはようございます」
  の独立行は入らない (入れると 4 行で契約違反)。brief 自体の存在が挨拶という割り切り。
- **全データ源が死んだら送らない**: compose が空文字列になったら RuntimeError で Job 失敗
  (空の挨拶で沈黙を偽装しない)。部分的欠損はその行ごと省く (「不明」埋め文字より正直)。
- backup 鮮度は pvc_usage[].backup_listing の**最新 1 本のみ**を見る (immich のみ listing
  在り)。stale 判定は >36h (毎日走る backup が正常時 ~21h 由来のため「1 回分取りこぼし」
  検知線)。境界ちょうどは鳴らさない。
- health 比較は history/{昨日}.jsonl の最終行 vs latest.json。history ファイル自体が
  無い日は比較不能として summary 表示に落とす (障害扱いにしない)。
- **commits API は per_page=100 の 1 ページのみ**: 100 件超の merge 日は過小集計の可能性
  (ログに出す)。現実のピーク (81 commits fetch / 19 merges, 08-22) では余裕。
  ページング追従が必要なら別論点で。

### 次のセッションへの一言

- verify 全項目 green 済みなので残務はレビュー対応の想定。merge 後は ArgoCD 適用で
  CronJob が作られ、翌朝 08:00 JST 初回実行。手動先行テストは
  `kubectl create job --from=cronjob/openclaw-morning-brief <name> -n autopilot`。
- rules.json は ruleset の人間レビュー必須パス (ファイル冒頭注記) — レビューで
  時間がかかる可能性のある唯一の変更。
- 発見: なし (スコープ外の問題には遭遇しなかった)。

## 2026-08-23 session 3

レビュー指摘なし (「reviewer セッションが verdict を書かなかった」) で verify 全項目 green 継続のため、
自己レビューを実施し 2 件の欠陥を発見・修正した。

### やったこと

1. **Bug 修正: 前日 history が無い日の健全性行が擬似追加に化ける**
   - `brief_lines` が prev_apps をそのまま `health_changes` へ渡していたため、prev=None
     (前日 history ファイル無し = reporter 停止日など) のとき全アプリが
     「coder ?→Healthy」の擬似的な新規出現として列挙されていた。session 2 の意図
     「history 無い日は summary 表示に落とす」は `line_health` 単体では実現しているが、
     `brief_lines` 経由では死んでいた (summary 枝は changes==[] のときしか発火しないが、
     prev=None では changes が非空になる)。dry-run の実データでは偶然 prev があり顕在化せず。
   - 修正: `prev_usable = bool(prev_apps)` (None も [] も比較不能扱い) で変化計算を
     スキップし summary 表示へ。出力文面自体を assert する回帰テスト追加 (28 tests)。
2. **Bug 修正: last_json_line が壊れ行で即 None を返す**
   - docstring「末尾から辿り、最初に JSON として読めた行を返す」に対し、実装は最初の
     壊れ行で即諦めていた。docstring 通りに壊れ行を飛ばして前を辿るよう修正
     (末尾の壊れ行の直前にある完全スナップショットを「前日の最終状態」に使える)。
     既存テストは単一行入力のみで影響なし。フォールバックのテスト追加。
3. verify 自力実測: unittest 28 tests OK / `kubectl kustomize apps/openclaw` 通過 /
   validate.py 0 error / live API で --dry-run 再確認 (3 行出力、送信なし)。

### 分かったこと / 決めたこと

- 純関数の契約テストだけでは組み合わせの死に枝 (summary 枝が brief_lines 経由では
  決して発火しない) を検出できない。「行生成器の各経路を通したときの出力文面」を
  compose レベルで assert するテストが必要だった (追加済み)。
- CronJob は openclaw-credentials Secret 依存。external-secret.yaml 注記 (2026-08-22)
  では TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID が Doppler 未登録の可能性があり、
  未登録なら Pod は CreateContainerConfigError で fail-loud する (仕様どおり)。
  merge 後・初回送信前に Secret 同期状態を確認すること。

### 次のセッションへの一言

- 既知の未解決問題なし。残務はレビュー対応の想定。
- 発見: ops/validate.py の warning 11 件 (backlog refs が指す ops/dashboard/* 不在 +
  todo 0 件警告) — 本プロジェクト外の既存問題。curriculum が拾うべきだが P-0174 とは無関係。

## 2026-08-23 session 4

レビュー指摘なし (verdict 不在)・verify green 継続のため、もう 1 回フレッシュ目で
実装と「外の世界との接合部」を突き合わせる自己レビューをしたら **機能の主信号が
実質死んでいる統合バグ** を見つけて修正した。

### やったこと

1. **Bug 修正 (重大): 健全性の「前日比」が朝の実行では約 30 分前との比較になっていた**
   - history ファイル名は reporter が **UTC 日付** で付ける (report.py の generated_at は
     UTC strftime、`history_path = ...format(generated_at[:10])`。実データ確認:
     history/2026-08-22.jsonl の中身は 2026-08-22T00:00Z〜23:30Z の 48 行)。
     一方 morning_brief は `day` = JST 昨日 で `history/{day}.jsonl` を取り **その最終行**
     を前日状態にしていた。JST = UTC+9 なので毎朝 08:00 JST (= 23:00Z) 実行時点では
     要求したファイルは「今日の UTC 日」の追記中ファイルで、最終行は約 30 分前の
     スナップショット。つまり (b) 健全性行はほぼ常に「変化なし」になり昨日の変化は
     永遠に見えない。session 2/3 の dry-run が偶然変化を見せたのは実行時刻が昼過ぎ
     (UTC 日が切り替わった後) だったからで、cron 本番時刻では顕在化しなかった。
   - 修正: 「JST day が終わる瞬間」(= 翌日 00:00 JST = 前日 15:00Z) より前の最新
     スナップショットを各行の generated_at で選ぶ (`jst_day_end_utc` +
     `prev_applications`)。境界跨ぎの欠損に備え day とその前日の最大 2 ファイルを
     見る (`jsonl_docs` で全行を dict 化)。generated_at 読めない行 /
     applications が list でない行は選考対象外。1 ファイルの最終行では絶対に
     正しい「前日の最終状態」にならないのがこの bug の核心。
   - 実データ回帰: 選ばれる baseline は history/2026-08-22.jsonl の 14:30Z 行で、
     openclaw / version-watcher がそこに居ないため dry-run の「openclaw ?→Healthy」は
     正しい正直な出力だった (旧コードだと 07:30Z 行と比較して「変化なし」に潰れていた)。
2. **Bug 修正: github_get がネットワークエラーで Job 全体を落としていた**
   - except は HTTPError のみ。タイムアウト・DNS 失敗等 (URLError / socket.timeout =
     OSError の一族) は素通りして fetch_sources を貫通 → 1 ソースの瞬断で Job 失敗 =
     brief 全体が消える。docstring が謳う「1 ソースの失敗で全体を止めない」と不一致。
   - 修正: `(OSError, http.client.HTTPException)` も握って status 文字列を返す
     (呼び出し側は ==200 以外をログして欠かすだけ)。send_telegram は握らない
     (送信失敗は Job を落として正しい)。TimeoutError/ConnectionResetError を
     urlopen に差し替えて例外経路を実走確認済み。
3. 強化: `brief_lines` の prev_usable を `isinstance(prev_apps, list)` 追加
   (非 list を渡すと dict 反復で空 map になり全アプリ「?→X」の疑似追加になる。
   fetch 側では起こらないが純関数の契約「壊れない」を守る)。
4. verify 自力実測: unittest **32 tests OK** / `test -f` green /
   `kubectl kustomize apps/openclaw` 通過 / validate.py 0 error /
   live API で --dry-run 再確認 (3 行出力・送信なし)。

### 分かったこと / 決めたこと

- **純関数テストは「自分の手で作ったデータ」しか食べない**: 今回の UTC/JST bug は
  composer の契約は全部満たした上で、外部 (reporter) の命名規約という接合面で起きた。
  接合部の疑いは本物のデータ (`git show origin/ops-health-report:...`) でのみ潰せる。
- 前日比の意味論: baseline = 「JST 昨日の終わりより前の最新」= 昨晩から今朝までの
  変化を見せる (「寝ている間の変化」)。PROGRESS session 2 の「history/{昨日}.jsonl の
  最終行 vs latest.json」の意図を timezone 分だけ正しく直した形。仕様変更ではない。
- last_json_line はこの修正で使われなくなったため削除した (test も差し替え)。

### 次のセッションへの一言

- 既知の未解決問題なし。残務はレビュー対応の想定。merge 後・初回送信前に
  openclaw-credentials Secret 同期確認 (TELEGRAM_BOT_TOKEN 未登録なら
  CreateContainerConfigError で fail-loud、仕様どおり) は session 3 から継続の宿題。
- 発見: なし (今回の修正はすべて本プロジェクトのスコープ内)。

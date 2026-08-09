# P-0026 — セッションが上限で死んだのか詰まったのかを見分ける

## 目的

VISION の第一原理「ループが止まらないこと」が、いま最も直接的に破れている。`ops/runner/runner.py:98`
が `claude` を `stderr=subprocess.DEVNULL` で起動しているため、死因の記録が「3 回連続で異常終了」
しか残らず、**アカウントのセッション上限 (器の外側の事実) と本当の実装詰まりが器から同じ顔に見える**。
2026-08-08 は 26 セッション / 名目 $50.9 を使ってプロジェクト 0 件前進、P-0023 と P-0025 が両方
stalled になった (同じ案を作り直して再採択した往復も、死因が読めないことの帰結)。
**死因を残させ、上限は「待って再開」に落とす。**

## 受入チェックリスト

initializer が実測した結果、**5 項目とも現時点で failing** (2026-08-09、`project/p-0026` の
checkout で、リポジトリルートから実行。rc は順に 1/1/1/1/1)。

- [ ] `grep -q 'stderr=subprocess.DEVNULL' ops/runner/runner.py && exit 1 || exit 0`
  — stderr を捨てる起動が残っていないこと。現在 `runner.py:98` に 1 箇所あるので rc=1。
    **`Session.run()` の `Popen` から DEVNULL を消すのが本体**で、grep は消えたことの確認にすぎない。
- [ ] `python3 -m unittest ops.runner.tests.test_exit_reason`
  — 分類の単体テストが存在して green。現在は `ModuleNotFoundError` (`ops/runner/tests/` が無い)。
    ファイルは `ops/runner/tests/test_exit_reason.py`、**`ops/runner/tests/__init__.py` も要る**
    (このコマンドはパッケージ import 形式であり discover ではない)。
- [ ] `python3 -c "... classify_session_failure('Claude AI usage limit reached') == 'usage_limit' and (...)('something else entirely') == 'unknown'"`
  — 純関数が `ops.runner.runner` のモジュール直下に在り、上限文字列を拾い、
    **知らない文字列を勝手に分類しない**こと。現在は関数が無いので `ImportError`。
- [ ] `python3 -c "import inspect ...; 'failure_kind' in s and 'stderr_tail' in s and 'waiting_quota' in s"`
  — runner.py のソースに 3 語が現れること。現在 0 箇所。**grep 同然の弱い検査**なので、
    語を置くだけでは DoD を満たさない。下の設計方針 (3)(4) を実装して自然に満たすこと。
- [ ] `grep -q 'usage_limit' ops/memory/substrate.md`
  — 意味記憶への記録。現在 0 箇所。

5 項目ともクラスタ・Discord・ネットワークに依存せず、ローカル checkout だけで判定できる。

## 設計方針

### 前提 (調べて分かったこと)

- 死因が消える現場は `Session.run()` (runner.py:84-157) の 1 箇所だけ。`claude` の Popen は
  `stdout=PIPE, stderr=DEVNULL`。stdout は reader スレッドが Queue に積み、transcript (PVC) に tee
  しつつ `result` イベントから usage を拾う。`proc.returncode != 0` を後から見て `outcome="error"`
  に落としている (runner.py:149-152、レビュー指摘 [14] の産物) — **ここが「異常終了」の唯一の情報**。
- `outcome` の消費側は `mode_worker()` の while ループ (runner.py:373-414)。`error` が 3 連続で
  `write_result("error", error="claude セッションが 3 回連続で異常終了")` (outbox の incident 本文は
  これがそのまま出ている)。`run_session()` は `outcome` 文字列しか返さない (runner.py:253-265)。
- `write_result()` は `**kw` をそのまま result.json に載せる (runner.py:267-272)。**追加フィールドは
  スキーマ変更なしに書ける。**
- **usage が取れないセッションは 50,000 トークンとして計上される** (runner.py:155-156)。上限で
  即死したセッションは実質 0 消費なのに、これで soft cap (3M) を 60 回で食い潰す。上限リトライを
  素朴に足すと**待っている間に予算が溶ける**。
- **heart 側は `waiting_quota` を知らない。** `reconcile.decide()` の `active` 枝
  (reconcile.py:211-279) が見る result state は `ready_for_review` / `budget_exhausted` /
  `spec_error` / `error` / `stalled_inactive` の 5 つだけ。それ以外の state が来ると
  どの分岐にも当たらず、`jobs is None` → `not p.get("job")` → `job is None` →
  `job.get("active")` → `job.get("failed")` の梯子も、**Job が正常終了 (succeeded) している場合は
  すべて false** になる。結果 **プロジェクトは `active` のまま永久に固まり、result.json も
  consume されずに残る** (heart.py:167-179 の consume_* は action が出たときだけ動く)。
  上限対策を入れて逆にループを止めては本末転倒なので、**reconcile 側の最小分岐は本 PR の範囲に含める**
  (同じ論点「上限は停滞ではない」の裏表)。
- `spawn.create()` は Job 名に `attempt`(=`spawn_count`) を含めるので、`spawn_runner` の再発行で
  新しい Job が立つ (heart.py:110-123)。Job は `backoffLimit: 0` / `activeDeadlineSeconds: 259200`
  (spawn.py:112-113) — つまり Job の生存時間より `rules.runner.session_max_seconds` (7200) の方が
  先に効く。DoD の「Job の生存時間を超えない範囲」は **7200 秒を待機予算の上限**と読む。
- テストの置き場: CI (`ops` job) が回すのは `python3 -m unittest discover -s ops/heart/tests -t .`
  の 1 行だけ (`.github/workflows/ci.yml:151`)。**`ops/runner/tests/` は放っておくと CI で走らない。**

### (1) stderr を捨てない — `Session.run()`

- `stderr=subprocess.PIPE` にし、**stdout とは別のデーモンスレッドで読み切る**。
  読まずに放置すると 64KB のパイプバッファが埋まって claude 側が書き込みでブロックし、
  無活動 kill まで固まる (今より悪化する)。保持は `collections.deque(maxlen=...)` で末尾のみ。
- **stderr の到着で `last_event` を更新しない。** 更新すると進捗の無いスピナー/警告だけで
  無活動 kill (`inactivity_kill_seconds`) が永久に発火しなくなる。活動の定義は今まで通り stdout。
- `proc.kill()` した経路でも stderr スレッドを合流させてから末尾を確定する
  (kill 後に `proc.wait()` は既にある)。合流は短い timeout 付きで、待ち切れなければ
  その時点の deque の中身を使う (診断が本体を止めない)。
- 上限メッセージが stderr でなく stream-json 側 (`result` イベントの `subtype`/`error`/`result`
  フィールド) に出る CLI 版もありうる。**分類の入力は「stderr 末尾 + 最後の result イベントの
  エラー文字列」を連結したもの**にし、どちらに出ても拾えるようにする (純関数の署名は spec 通り
  `classify_session_failure(stderr_tail: str) -> str` のまま。連結は呼び出し側の責務)。

### (2) 純関数 `classify_session_failure(stderr_tail) -> str`

`ops.runner.runner` のモジュール直下 (クラス外) に置く。返すのは
`'usage_limit' | 'auth' | 'network' | 'unknown'` の 4 値のみ。**判定順は
usage_limit > auth > network > unknown** で固定する (429 系は上限に寄せる。文言が重なるため順序が意味を持つ)。
パターンはモジュール定数の表 (正規表現のリスト) にして、テストからも参照できるようにする。

| kind | 拾う文字列 (小文字化して部分一致 / 正規表現) |
|------|------------------------------------------|
| `usage_limit` | `claude ai usage limit reached` (CLI は `…reached\|<epoch>` の形で reset 時刻を付けることがある)、`usage limit`、`5-hour limit`、`limit reached ∙ resets`、`rate_limit_error`、`429` + `rate limit` |
| `auth` | `invalid api key`、`authentication_error`、`oauth token`、`please run /login`、`401`、`unauthorized`、`forbidden` |
| `network` | `enotfound`、`econnrefused`、`etimedout`、`econnreset`、`socket hang up`、`fetch failed`、`getaddrinfo` |
| `unknown` | 上記いずれにも当たらない / 空文字列 |

**注意: 上限の実文字列はこのリポジトリに残っていない** (DEVNULL で捨ててきたのが本プロジェクトの
発端であり、journal にも記録が無い)。上表は claude CLI の既知の出力形を根拠にした候補である。
テストは上表の各文字列を固定値として並べ、**「この文字列が来たらこう分類する」という契約を
テストで宣言する**。実際に別の文言を観測したら、その回の result.json の `stderr_tail` を証拠に
表とテストへ追記する — そのための `stderr_tail` である。この経緯を関数の docstring に 1〜2 行残すこと。

reset 時刻は別の小関数 `parse_usage_limit_reset(text) -> datetime | None` に分ける
(`…reached|1754697600` の epoch を拾う。取れなければ None → 既定待機)。分類と時刻抽出を混ぜない。

### (3) result.json に `failure_kind` / `stderr_tail`

- `Session.run()` の戻り値を `(outcome, usage, info)` に拡張する。`info` は
  `{"failure_kind", "stderr_tail", "reset_at"}`。`run_session()` は今まで通り `outcome` を返しつつ
  `self.last_session = info` に置く (呼び出し側 5 箇所の書き換えを最小化する)。
- `write_result()` の呼び出しのうち **異常終了系** (`error` / `stalled_inactive` /
  initializer 失敗 / curriculum の各 error) に `failure_kind=` と `stderr_tail=` を渡す。
  `error="claude セッションが 3 回連続で異常終了"` も `failure_kind` を含む本文に直す
  (incident 通知の本文がこれをそのまま出すため — 通知が読めるようになるのが目的の半分)。
- **マスク**: `stderr_tail` は末尾 2000 文字に切る**前**にマスクする。
  (a) 環境変数 `AUTOPILOT_GITHUB_TOKEN` / `CLAUDE_CODE_OAUTH_TOKEN` の**実値そのものの literal 置換**
  (最も確実)、(b) `ghp_…` / `github_pat_…` / `sk-ant-…` / `Bearer <...>` / `x-access-token:<...>` の
  正規表現置換。置換後は `***` 等に潰す。マスクも純関数にしてテストする
  (`mask_secrets(text, env)` 相当。env を引数で受ければテストが os.environ に触らずに済む)。

### (4) `usage_limit` は「3 回連続 error」に数えず、待って再開する

`mode_worker()` の while ループ (runner.py:400-414) の `elif outcome == "error"` の**手前**に
usage_limit の枝を置く。

- `consecutive_error` を進めない。`consecutive_inactive` も触らない (別軸)。
- **待機時間** = `reset_at - now` (取れなければモジュール定数の既定、例 `DEFAULT_QUOTA_WAIT_SECONDS`
  = 900 秒程度) + 小さなマージン。
- **待機予算**: この runner プロセスで待機に使ってよい総量を `rules["runner"]["session_max_seconds"]`
  (7200) とし、累積で管理する。今回の待機が残り予算に収まるなら `time.sleep` して同じループを
  回し直す (= 同じセッションの再開)。収まらないなら
  `write_result("waiting_quota", failure_kind="usage_limit", stderr_tail=..., resume_after=<ISO8601>)`
  を書いて **rc=0 で終える** (`stalled` にしない)。
- **rules.json は触らない** (人間レビュー必須パス)。既存キーを読むだけ。待機の既定値・回数上限は
  runner.py のモジュール定数にする (`reconcile.REVIEW_TIMEOUT_HOURS` と同じ流儀)。
- **予算の空回りを止める**: `usage_limit` と判定したセッションは runner.py:155-156 の
  「トークン不明なら 50,000」の概算を**適用しない** (実消費ゼロの即死に 50k を計上すると、
  待って再開する前に soft cap が尽きる)。`budget.sessions` の加算は**残す** —
  `max_sessions_per_project` が無限ループの最後の歯止めなので外さない。
- 待機中も Job は Running のままなので、heart から見れば `job.active` = 真で drift もしない。
  ただし**沈黙は禁物**: `log()` で「usage_limit: <n>s 待機して再開する (reset_at=…)」を必ず出す
  (heartbeat ログが唯一の外からの観測経路)。

**heart 側の最小分岐 (`ops/heart/reconcile.py`)** — 新 state を作らず、`active` 枝に 1 本足す:

- `result.state == "waiting_quota"` → `consume_result` を出し、**`stalled` にしない**。
  `p["quota_wait_until"] = result["resume_after"]` を書いて `active` のまま待つ。
- 次以降のビートで `now >= quota_wait_until` になったら `spawn_runner`(respawn) を出し、
  `quota_wait_until` を消す。`breaker` 中と `running >= max_concurrent` のときは出さない
  (既存の spawn 抑制と同じ扱い)。
- `statefiles.PROJECT_STATES` / `validate_projects()` / `notify.IMMEDIATE_TYPES` は**変えない**。
  通知は原則出さない (上限待ちは障害ではない)。人間に見せたいなら既存の projects.json の
  フィールドとダッシュボード経由で足りる。
- `ops/heart/tests/test_reconcile.py` に遷移の表を足す (既存の流儀: ヘルパで dict を組み立て、
  遷移を表として書く)。**「waiting_quota で stalled にならないこと」と「時刻到来で respawn すること」**
  の 2 本が最低線。

### (5) `ops/memory/substrate.md`

「観測経路」の後ろか、新設の節 (例「claude セッション / 利用上限」) に、**`verified_at` と出典付き**で:
上限は器の外側の事実であり停滞ではないこと、対話セッションと器が同一サブスクリプションを共有する
構造要因、`usage_limit` の判定文字列と result.json の `failure_kind` / `stderr_tail` に残ること。
`ops/memory/README.md` は「書き手は consolidation の PR のみ」としているので、**P-0015 と同様に
「spec の DoD (5) が名指しで要求した例外」である旨を 1 行添える**。

### (6) CI 配線

`.github/workflows/ci.yml` の既存 `ops` job に 1 ステップ足す
(`python3 -m unittest discover -s ops/runner/tests -t .`)。既に必須チェックに入っている job への
ステップ追加なので **ruleset の変更は要らない**。`.github/` は人間レビュー必須パスだが、
`ops/runner/` を触る本 PR はどのみち人間のレビュー待ちになる (auto-merge されない)。
PR 本文にその旨を書くこと。

## やらないこと

- **`ops/rules.json` / `ops/models.json` の変更。** 人間レビュー必須パスかつ単一情報源。
  待機の既定値・上限は runner.py のモジュール定数に置く。既存キーは読むだけ。
- **`auth` / `network` に基づく制御の変更。** 今回は**記録するだけ**。認証失敗の即時打ち切りや
  ネットワーク断のバックオフは、実際の `stderr_tail` が集まってから別プロジェクトで決める
  (1 PR 1 論点、CHARTER §4)。`usage_limit` だけが制御を変える。
- **器専用 credential の分離 (seeds #15 の恒久策)。** 人間の鍵作業を含む別件。
  本 PR は「同一サブスクリプション共有」を所与として、その症状を識別できるようにするだけ。
- **`statefiles.PROJECT_STATES` への新 state 追加 / `validate_projects()` の変更。**
  `waiting_quota` は **result.json の state** であって projects.json の state ではない。
  プロジェクトは `active` のまま待つ。
- **Discord 通知の新型・新しい通知経路。** 上限待ちは障害ではないので原則通知しない。
  `notify.py` / `IMMEDIATE_TYPES` を触らない。
- **breaker (`rules.breaker.daily_cost_usd`) やコスト計上の見直し。** 名目コストの扱いは別論点。
  本 PR が触る予算の話は「上限即死セッションに 50k の概算を付けない」の 1 点だけ。
- **transcript の構造化・critic 連携 (seeds #7)。** `stderr_tail` を result.json に残すところまで。
  集計や可視化は別プロジェクト。
- **`Session` の stdout 処理 / 無活動 kill の閾値・仕組みの変更。** stderr を足すだけで、
  活動の定義 (stdout イベント) は変えない。
- **`apps/` 配下への変更。** `touches_apps: false`。soak も要らない。

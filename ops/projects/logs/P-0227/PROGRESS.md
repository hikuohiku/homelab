# P-0227 PROGRESS

## セッション 1 (2026-08-23)

- initializer: PROJECT.md 作成。verify 3 項目は全て failing を実測 (rc=1)。
- worker への引き継ぎ: DoD(3) で層を選んだら、**選んだ層と他の層を避けた根拠をこのファイルに書くこと** (spec の要求)。証跡は `raw-*` に生のまま保存する

## セッション 2 (2026-08-23, worker)

### やったこと

1. **検死完了** → `failures.md` に `root_cause: auth` で断定。
   実測 5 回の死 (09:42 / 10:41 / 14:11 の generate 死 + 17:19 / 18:14 の judge 死)
   はすべて同一パターン: opencode が `APIError "Provider finish_reason:
   network_error" (isRetryable=true)` で内部リトライ (~75s・6回) を使い切って即死
   → classify は unknown → 直後の API プローブが **HTTP 401** → auth。
   同一鍵の隣接実行は成功しているので恒久的な鍵無効ではなくプロバイダ側の瞬間的な
   拒否窓。health 18:30Z の「3 Pod」は ttlSecondsAfterFinished=21600 で GC されずに
   残っていた最新 3 本 (14:11/17:19/18:14)。09:42/10:41 は既に消えていた
2. **証跡を raw-* として 10 ファイル保存** (result.json 5 + transcript 5、いずれも cp のみ)。
   pod log / events は取得不能 (このセッションに kubeconfig 無し + Pod 消滅済み) —
   failures.md に正直に記載済み
3. **歯止めを実装 (runner 層)**: `mode_curriculum` を `run_curriculum_phase()` 経由に
   変更。発火条件は純関数 `curriculum_next_action()` (runner.py) に集約:
   - usage_limit → `quota_wait_or_yield` を待機 (worker の P-0026 流儀を curriculum に
     初配線。PROJECT.md 前提 (a) の未適用箇所を潰した)
   - auth / network / unknown / timeout / 無活動 kill → 連続 3 回 (`CURRICULUM_MAX_CONSECUTIVE_ERRORS`)
     まで有界リトライ。judge の再試行は同じ Pod 内なので /work/proposals.json が生き、
     20〜30 万トークンの生成し直しを避けられる
   - completed だが産物無し → 即 give_up (再試行は高価なだけ。heart の再 spawn に任せる)
4. **テスト**: `ops/tests/test_curriculum_resilience.py` (12 tests)。実測の error
   イベント JSON を引用して「engine message 単体では unknown → プローブ 401 で auth」
   の根拠チェーンと、上記の発火条件マッピングを固定

### verify 実測 (このセッション終了時)

- v1 failures.md + root_cause 行: **rc=0**
- v2 raw-* 存在: **rc=0**
- v3 unittest ops.tests.test_curriculum_resilience: **OK (12 tests)**

### 層の選択と根拠 (spec DoD(3) の要求)

**選んだ層: runner 層** (mode_curriculum のフェーズ内 bounded retry + quota 待機配線)。

- **断定された死因が engine セッションの瞬間死** (auth/401、隣接実行は成功) であり、
  これを直接潰せるのはセッションを起こす側だけ。prompt 層は死因が構造系である以上
  効かない (エンジンが出力を返す前に死んでいる。「締めの義務」の文言があっても
  今回の 5 死は防げなかった — 文言に到達する前に API が拒否した)
- **heart 層を避けた根拠**: heart は今回の死を既に検知できている
  (result.json state=error → incident 通知 → min_interval 1h 置きに再 spawn、
  reconcile.py:559-567)。つまり heart 層には検知の穴が無く、追加しても価値が無い。
  「proposals.json 未写出のまま X 分」型の heart 層検知が効くのは前提 (b) の
  「result.json 自体が無い死」だが、それは今日の 3 Pod の死因ではない
  (全員 result.json を書けた)。spec が指定するのは「断定された死因に対する」歯止め
- **runner 層の追加効果**: judge フェーズ死の実被害 (生成済み案の喪失) が
  同一 Pod 内の再試行で消える。heart 層や prompt 層ではこれは実現できない

### 分かったこと / 次のセッションへの引き継ぎ

- **verify はもう green のはず。** wrapper の受入実測が 3 項目とも ok になれば
  レビューへ進む段階。もし差し戻されたらレビュー指摘を最優先で直す
- **罠**: failures.md の `root_cause:` は行頭にないと grep ('^root_cause:') に
  引っかからない (初回 v1 だけ rc=1 になった実害)。追記するときは行頭に書く
- **罠**: `/data/projects/system/result.json` は常時存在するとは限らない
  (走行中 or consume 済み)。過去の result は `/data/projects/system/processed/
  <ts>-result.json` にある。transcript は `/data/transcripts/curriculum/`
- **未解決の仮説** (failures.md「残る不確実性」より): プローブの 401 とエンジンの
  stream 切断の齟齬は完全には閉じていない。次に同型の死を観たら probe_status /
  stderr_tail を証拠に fixture 追記を検討 (substrate の規律)。本物の上限死 (429)
  の文言は依然未観測
- **発見 (spec 外、ここに書くだけ)**: (1) opencode には isRetryable=true の内部
  リトライがある (~6 回)。runner 側の歯止めと二段構えになる。(2) curriculum の
  result state `waiting_quota` は reconcile.py に専用分岐が無い (error/done のみ)。
  今回は quota 待機予算が session_max_seconds (=activeDeadline と同値) なので
  実質 in-process sleep に吸収され waiting_quota 書き出しはほぼ起きないが、
  起きた場合は consume されずに result.json が残置される (害は無いが掃除されない)。
  (3) 08-22 の「completed なのに proposals.json 無し」2 回は今回の対象外。
  再発するなら judge/generate プロンプトの産物契約の検査を prompt 層で検討

### スコープ外で見つけたこと (curriculum が拾う)

- PROGRESS 上記「(2)(3)」参照。監視スタック・FAILURE_PATTERNS への新パターン追記は
  PROJECT.md「やらないこと」のとおり触っていない

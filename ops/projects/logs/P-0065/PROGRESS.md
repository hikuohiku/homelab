# P-0065 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### 2026-08-22 セッション1

**やったこと**: 両方の受入項目に対応する実装を一括で入れた (2つの verify は同じ変更で緑になる
ので分割する意味が薄いと判断)。

1. `ops/heart/liveness.py` を新設。純関数 `is_stale(heartbeat_at, now, max_age)` /
   `max_age_seconds(beat_seconds)` と、`heartbeat.json` を読んで判定する `check()`、
   exec probe から呼ぶ `main()`。しきい値 (`STALE_MULTIPLIER=5`, `STALE_MARGIN_SECONDS=60`)
   はこのファイル 1 箇所だけに置いた。
2. `ops/heart/tests/test_liveness.py` を新設 (unittest.TestCase、9 テスト:
   fresh/stale/境界値/heartbeat.json 欠落/壊れた JSON/`at` フィールド欠落)。
   `python3 -m unittest discover -s ops/heart/tests -t .` で green (153 テスト全体も green)。
3. `apps/autopilot/heart-deployment.yaml` に `livenessProbe` (exec) を追加。
   `python3 -c "sys.path.insert(0,'/work/repo'); from ops.heart.liveness import main; main()"`
   を呼ぶ。`initialDelaySeconds=300 / periodSeconds=30 / timeoutSeconds=15 /
   failureThreshold=3` は実測の裏付けが無い安全側の初期値 (PROJECT.md の設計方針どおり、
   PR 本文にもその旨を明記すること — まだ書いていないので次のセッション or PR 作成時に忘れずに)。
4. exec コマンドは `/work/repo` に手動で `HEART_DATA_DIR` / `HEART_BEAT_SECONDS` を渡して
   fresh/stale 両方を手動実行し、rc=0/1 が想定通りになることを確認済み。
   `kubectl kustomize apps/autopilot` も通ることを確認した。

**分かったこと**:
- `python3 -m pytest ops/heart/tests -k liveness -q` はこのセッションのサンドボックスでも
  `No module named pytest` で失敗する (pip も無い、ネットワーク越しの install も未確認)。
  PROJECT.md の initializer が既に指摘していた既知の制約で、CI (`ci.yml:56`) も
  `unittest discover` しか回していない。**これは新しい発見ではない** — 次のセッションが
  「pytest が無い」を再調査する必要は無い。wrapper 側の実 verify 環境に pytest が入っている
  ことを期待するしかない。もし wrapper 実行でもこの verify が red のままなら、
  spec の verify コマンド自体が実行不能という話になるので、その場合は curriculum
  (仕様側) の問題として書き残す必要がある。
- REPO_DIR は heart-bootstrap.sh の shell 内 `export` に留まり、Pod の `env:` には
  乗っていない (`heart.py` の `main()` も `os.environ.get("REPO_DIR", os.getcwd())` で
  cwd フォールバックしている)。そのため exec probe 側では `/work/repo` をハードコードで
  デフォルトにした (bootstrap.sh の既定と同じ値)。Pod env に REPO_DIR を追加する手も
  あったが、スコープ外の変更を増やしたくなかったので見送った。

**次のセッションへの一言**:
- 受入 2 項目とも実装は完了しているはず。まず wrapper の実測 (このセッション冒頭で渡される
  JSON) を見て、2 項目とも green になっているか確認すること。green ならこのプロジェクトは
  完了 — PR 本文に「しきい値は実測の裏付けが無い安全側の初期値」である旨を明記されているか
  だけ確認してほしい (CHARTER §4 が縛る変更に対してこれを要求している)。
- もし pytest 側の verify がまだ red なら、原因が「テストが無い/落ちている」なのか
  「pytest コマンド自体が実行不能」なのかをまず切り分けること。後者なら実装の問題ではない。

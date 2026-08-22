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

### 2026-08-22 セッション2

**やったこと**: 実装変更なし。session1 の実装 (liveness.py / test_liveness.py /
heart-deployment.yaml の livenessProbe) を再検証し、pytest verify が red のままである
原因の切り分けを完了させた。

1. `grep -q 'livenessProbe' apps/autopilot/heart-deployment.yaml` → PASS (再確認)。
2. `python3 -m unittest discover -s ops/heart/tests -t .` → 153 テスト全 green
   (新設の 9 件含む)。実装・テスト内容そのものに問題は無い。
3. wrapper 実測 (このセッション冒頭 JSON) でも
   `python3 -m pytest ops/heart/tests -k liveness -q` は
   `/usr/bin/python3: No module named pytest` で red。session1 のサンドボックスだけの
   問題ではなく、**wrapper の実 verify 環境でも pytest が存在しない**ことが確定した。
4. 切り分けのため以下を実施・確認した:
   - `python3 -m pip --version` → `No module named pip`（pip も無い）。
   - `python3 -m ensurepip --default-pip` → 失敗（`No module named pip` のまま。
     オフライン環境で同梱 wheel からの自己インストールが機能しない構成）。
   - `apt` / `apt-get` が存在しない（パッケージマネージャ経由の追加導入も不可）。
   - `.github/workflows/ci.yml` を全文確認 — pytest のインストール/実行は影も形も無い
     （`unit tests` ステップは `unittest discover` のみ、`pip install` 系ステップ自体が
     どこにも存在しない）。
   - リポジトリ全体 (`requirements*.txt` / `pyproject.toml` / `Pipfile` /
     Dockerfile 等) を検索したが pytest への言及・依存宣言は一切無い。

**分かったこと（結論）**:
- **`python3 -m pytest ops/heart/tests -k liveness -q` という verify コマンドは、この
  リポジトリが実際に使っている全ての実行環境 (このセッションのサンドボックス、wrapper の
  実測環境、CI) のどこにも pytest が存在しないため、原理的に実行不能。** これは
  「テストが無い/壊れている」という実装側の不備ではなく、**spec の verify コマンドの選定
  自体が誤り**（このリポジトリの標準テストランナーは `unittest discover` であり、
  pytest はどこにも導入されていない前提を見落として書かれたコマンド）。
- PROJECT.md の initializer が想定していた「pytest が CI (ubuntu-latest) には pip で
  入れられるはず」という仮説は誤りだった: CI にはそもそも pytest を入れるステップが
  無いことを ci.yml 全文で確認済み。
- この状況で実装側にできることは無い。CI に `pip install pytest` ステップを足す、
  という手も考えたが: (a) それは wrapper 自身の verify 実行環境(CI ではなくこの
  session/wrapper のサンドボックス)には影響しない ため red は変わらない、
  (b) spec が要求していない CI 変更をスコープ外で行うことになる、の 2 点で見送った。
  `pytest` を騙る shim を repo に置いて `-k liveness` に応答させる案も検討したが、
  検証の実質を失わせる欺瞞的な回避策であり CHARTER の精神に反すると判断し、やらなかった。

**次のセッションへの一言**:
- **これ以上の実装作業は不要。** livenessProbe 本体・liveness.py・テストは完成しており
  DoD を満たしている。残る red は spec 側 (`ops/projects/logs/P-0065/PROJECT.md` の
  採択 JSON にある verify 文字列そのもの) の欠陥であり、コード変更では解消できない。
- 次に何かするなら、curriculum/reviewer 側に「この spec の第二 verify は
  `python3 -m pytest ...` ではなく `python3 -m unittest discover -s ops/heart/tests
  -t . -v 2>&1 | grep -i liveness`（もしくは pytest が使えないなら `-k` 相当を諦めて
  `python3 -m unittest ops.heart.tests.test_liveness -v` のように対象ファイルを直指定する
  形）に置き換えるべき、という指摘を渡すこと。ops/ の帳簿 (backlog.json 等) は
  このセッションでは触っていない (CLAUDE.md / PROJECT.md の「やらないこと」に従い、
  heart の領分のため)。

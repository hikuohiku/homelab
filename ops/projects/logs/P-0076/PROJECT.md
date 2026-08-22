# P-0065 — heart が詰まったら自分で気づけなくても k8s に強制再起動させる

## 目的

2026-08-12T22:25 (beat 3173) を最後に heart pod が fork 不能で完全停止したが、外部 watchdog
(P-0027, GitHub Actions) は9日以上正しく警報を出し続けたのに自動で直す経路が無く、人間が
2026-08-22 に手動で `restart-stamp`（`apps/autopilot/heart-deployment.yaml:33`）を書き換える
まで復旧しなかった。検知は機能した。直さない仕組みだけが無かった。`heart-deployment.yaml` に
`livenessProbe`/`readinessProbe` は1つも定義されていない。

heart 内部が詰まった（fork 不能を含む）とき、kubelet が自動でコンテナを再起動できるように
probe を足す。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**（2026-08-22、`project/p-0065` の
checkout で、リポジトリルートから実行）。

- [ ] `grep -q 'livenessProbe' apps/autopilot/heart-deployment.yaml`
  — heart Deployment に livenessProbe が定義されていること。実測 rc=1（`grep -c` = 0 件。
    このファイルには probe が 1 つも無い）。
- [ ] `python3 -m pytest ops/heart/tests -k liveness -q`
  — 心拍鮮度の判定ロジックにユニットテストが存在し green であること。実測: `ops/heart/tests/`
    に "liveness" を含むファイル・テストが無い。加えて **pytest 自体がこの initializer の
    サンドボックスに入っていない**（`No module named pytest`）。CI (`ci.yml:54-58`) は現状
    `python3 -m unittest discover -s ops/heart/tests -t .` だけを回しており、pytest は
    どこにも登場しない。この spec の verify がそのまま CI の前提になるとは限らないので、
    worker は着手時に CI 実行環境 (`.github/workflows/ci.yml`) に pytest が入っているか
    （runs-on: ubuntu-latest — pip で入れれば通るはずだが、setup ステップが無ければ
    `command not found` で rc≠1 の別の失敗になる）を先に確認すること。**pytest は
    unittest.TestCase を問題なく収集できる**ので、既存の `unittest.TestCase` 資産を捨てて
    書き直す必要は無い。ファイル名またはテストメソッド名に `liveness` を含めれば
    `-k liveness` の対象になる。

## 設計方針

### 前提（initializer が 2026-08-22 に実読した。調べ直さなくてよい）

- **heartbeat の実体**: `ops/heart/statefiles.py:163` の `write_heartbeat()` が毎ビート
  `ops-state` ブランチの `heartbeat.json`（`{"beat", "at", "writer"}`）を書く。この state
  ディレクトリは `Config.data_dir / "ops-state"` = **`${HEART_DATA_DIR:-/data}/ops-state`**
  で、`/data` は heart Pod にマウント済みの PVC (`heart-deployment.yaml` の `data` volume)。
  つまり `heartbeat.json` は git 越しではなく**コンテナのローカルファイルとしてそのまま読める**
  ので、exec probe から直接読める。
- **`write_heartbeat()` は `beat()` の最後の方で呼ばれる**（`heart.py:405`、`execute()` の後）。
  `beat()` は `run()` 側で try/except に包まれて例外を握りつぶし次のビートへ進む
  （`heart.py:439-443`）ため、**heart は例外を出しても死なずに回り続ける** — が、`beat()` の
  冒頭 `gitutil.sync_main()`（git のサブプロセス呼び出しが要る）で毎回死ぬような壊れ方
  （例: fork 不能）をすると `write_heartbeat()` まで到達できず、`heartbeat.json` の `at` が
  そこで止まったまま動かなくなる。**今回の事故はまさにこの形**。心拍の「新鮮さ」を見るのが
  最も筋が良い。
- **ビート間隔は `HEART_BEAT_SECONDS`**（`heart-deployment.yaml` で現行 `60`）。probe の
  しきい値はこれをハードコードせず、同じ env を probe 側でも読む（`config.py` の既定は 120
  なので、ハードコード両建てにすると乖離する）。
- **exec probe は kubelet がコンテナランタイム経由で新規プロセスを立てる**ため、heart
  本体プロセスの fork 不能とは独立に動く（fork 不能の原因がノード全体の資源枯渇なら probe
  の exec 自体も失敗しうるが、その場合も kubelet はプローブ失敗として扱い再起動に倒れるので
  安全側）。
- **exec probe に `workingDir` 相当のフィールドは無い**（`ExecAction` は `command` のみ）。
  `ops.heart.*` を `python3 -m` で呼ぶなら probe 側で明示的に `cd` する必要がある。
  `heart-bootstrap.sh` は `REPO_DIR`（既定 `/work/repo`）に clone して `cd` してから
  exec しているので、probe コマンドも同じ `REPO_DIR` を使うか、**repo チェックアウトに
  依存しない自己完結スクリプト**にするかの二択。後者のほうが「probe が repo の git 状態に
  引きずられて誤検知する」経路を作らずに済む。
- **同型の前例が repo に既にある**: `apps/coder/postgres.yaml` / `apps/immich/postgres.yaml`
  の `livenessProbe.exec.command: [pg_isready, ...]`。exec 形式の probe を足すこと自体は
  この repo で目新しくない。一方 `apps/autopilot/deployment.yaml`（旧 loop.sh 側）にも
  probe は無く、これは今回のスコープ外（下記「やらないこと」）。
- **CHARTER §4「縛る変更には実測か裏付けが要る」が probe の追加/閾値変更を名指しで例示している。**
  この PR は CI green になっても作成した回では merge しない（cooldown）。次の回が中断検知で
  拾い、issue #56 に新しい異議が無いことを確認してから merge する運用になる想定。
  worker は実装後に `enable_pr_auto_merge` を呼ばずに PR を開いたまま終えてよい（CHARTER の
  既存規則どおりで、この PROJECT.md で新設するルールではない）。

### 決めてあること（この方針で作る。変えるなら理由を PROGRESS.md に書く）

1. **判定ロジックは純関数として新設する**: `ops/heart/liveness.py` に、heartbeat の
   `at`（ISO8601, `statefiles.parse_iso` と同じ形式）と現在時刻・許容秒数を受け取り
   新鮮/陳腐を返す関数（例: `is_stale(heartbeat_at, now, max_age_seconds)`）を置く。
   `ops/heart/reconcile.py` が「判断は純関数だけ、テストは仕様」としている既存の流儀
   （README.md 「原則」節）に倣う。
2. **ユニットテストは `ops/heart/tests/test_liveness.py`**。ファイル名に `liveness` を含める
   ことで `pytest -k liveness` にも `unittest discover` にも同時にヒットする。ケースは
   最低限: 直近ビート内（fresh）／しきい値超過（stale）／`heartbeat.json` が存在しない・
   壊れている（stale 扱いにする — 「無い」を「元気」と誤読しない）。
3. **exec probe から呼ぶ実体は、repo チェックアウト非依存の小さいスクリプトにする**
   （`ops.heart.liveness` の純関数を import できるなら import して使い、できない実行文脈
   （probe 実行時に `/work/repo` が未 clone、または `sys.path` が repo ルートでない）では
   `heartbeat.json` を直接読んで自前でしきい値判定するフォールバックを持たせるか、
   probe コマンド自体を `sh -c 'cd "${REPO_DIR:-/work/repo}" && python3 -m ops.heart.<script>'`
   の形にして repo チェックアウトの存在を前提にする（`work` volume は同じコンテナに
   マウント済みなので、heart 本体が一度でも起動できていれば `/work/repo` は存在する）。
   どちらにするかは worker が実装しながら決めてよいが、**しきい値判定そのもの
   （何秒で stale とするか）は `liveness.py` の 1 箇所だけに書き、probe スクリプトと
   ユニットテストの双方がそこを参照する**（値の重複を作らない）。
4. **しきい値は `HEART_BEAT_SECONDS` の数倍 + 余裕**にする。git fetch/push を含む 1 ビートは
   ネットワーク次第で伸びることがあるため、ビート間隔ちょうどで stale 判定すると誤検知
   （正常に遅いだけなのに再起動）を招く。倍率の具体的な数値に実測の裏付けは無いので、
   「安全側に振った初期値」であることを PR 本文に明記する（CHARTER §4 の「縛る変更」節が
   要求する体裁）。
5. **`heart-deployment.yaml` に追加するのは `livenessProbe` のみ**（DoD が要求するのはこれ）。
   `initialDelaySeconds` は repo clone + 初回ビート分の余裕を持たせる（`heart-bootstrap.sh` の
   clone 完了前に `heartbeat.json` は存在しないため、根拠なく短くすると起動直後に
   crash loop する）。`failureThreshold` は 1 回の遅いビートで即再起動しない程度に余裕を持つ。
6. **既存の `restart-stamp` annotation は残す**。probe は「heart 内部が詰まった」ケースの
   自動化であり、「Pod を明示的に作り直したい」その他の運用上の理由（今回のような k8s
   write 権限が無い状況からの手動復旧）まで置き換えるものではない。

### ロールバック

追加のみの変更（`livenessProbe` フィールドと新規モジュール/テストファイル）。revert すれば
probe が消えて元の「詰まっても誰も直さない」状態に戻るだけで、データやスキーマは一切絡まない。

## やらないこと

- **旧 `apps/autopilot/deployment.yaml`（loop.sh 側）への probe 追加**。spec が名指ししているのは
  `heart-deployment.yaml` のみ。1 PR 1 論点。
- **`readinessProbe` / `startupProbe` の追加**。DoD が求めるのは「詰まったら再起動」であり
  liveness で足りる。startupProbe で起動猶予を別立てにする案は設計として有りだが、
  spec の verify が `livenessProbe` の存在しか見ていないので今回は livenessProbe 一本で完結させる。
- **`HEART_BEAT_SECONDS` や `reconcile.py` の状態機械そのものの変更**。probe はビート周期にも
  判断ロジックにも手を入れない。読むだけ。
- **fork 不能の根本原因調査**（node01 側の資源枯渇・PID 上限等）。今回は「詰まったら自動で
  再起動できるようにする」ところまでで、詰まる原因そのものを潰す調査は別論点。
- **memory limits の新設**。この事故と無関係。CHARTER 上も実測の裏付けなしに付けない方針は継続。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / CHARTER・VISION・`ops/memory/` の
  更新**。heart が直接 `main` に push するファイルでコンフリクトする（CLAUDE.md）。
  気づいたことは PROGRESS.md に書いて次（consolidation 等）に渡す。

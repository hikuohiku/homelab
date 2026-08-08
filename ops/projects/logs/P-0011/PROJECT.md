# P-0011 — ops-health-reporter を heart 対応にし、心臓の生存を再び観測可能にする

## 目的

VISION の第一原理は「ループが止まらないこと」なのに、その心臓（`autopilot-heart`）の生死を見る
計器が壊れている。`apps/ops-health-reporter/report.py` の `collect_autopilot_health()` は退役済みの
Deployment `autopilot`（replicas 0）と label `app=autopilot` を見ており、heart が死んでも
health レポートは死ぬ前と同じ文字列を出し続ける — 異常が定常状態に埋もれている。
直すだけでなく、同型の drift（T-0057 系で 4 回起票した）が二度起きないよう機械検査まで載せる。

**現物（2026-08-08T08:00:04Z の `origin/ops-health-report:ops/health/latest.json` の `autopilot` キー）**:

```json
{"deployment": {"replicas": 0, "readyReplicas": 0, "unavailableReplicas": 0},
 "pods": [], "heartbeat": {"error": "app=autopilot の pod が見つからない"}}
```

同時刻、heart は生きている（`/data/ops-state/heartbeat.json` が `{"beat": 14, "at": "2026-08-08T08:26:04Z"}`）。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**（2026-08-08、`project/p-0011` の
checkout で実行）。

- [ ] `python3 ops/check_health_reporter_target.py`
  — 新規スクリプトが存在し、`apps/autopilot/*.yaml` から機械抽出した Deployment 名・app ラベルと
    `report.py` が参照している名前・セレクタが一致すること。現在 exit=2（ファイルが無い）
- [ ] `grep -q 'autopilot-heart' apps/ops-health-reporter/report.py`
  — report.py の観測対象が heart に切り替わっていること。現在 exit=1
    （report.py 中の文字列は `deployments/autopilot` と `labelSelector=app%3Dautopilot` のみ）
- [ ] `grep -q 'check_health_reporter_target.py' .github/workflows/ci.yml`
  — そのチェックが CI（`ops` job）に配線されていること。現在 exit=1

**受入は 3 つとも repo 内で完結し、この runner Job の中で実行して判定できる**（クラスタ到達は不要）。
DoD (1) の「in-cluster のログで実測確認する」だけは別経路が要る（下記「実測の取り方」）。

## 設計方針

### 前提として実測した事実

- **heart は既に HEARTBEAT_RE と同じ書式を出している。** `ops/heart/heart.py:38` の
  `log()` が `print(f"[autopilot] {now_iso()} {msg}")`、`heart.py:316,324` が
  `iteration #{i} start` / `iteration #{i} end exit={rc} elapsed={elapsed}s` を渡す。
  `now_iso()`（`ops/heart/statefiles.py:38`）は `%Y-%m-%dT%H:%M:%SZ` で空白を含まないので
  `HEARTBEAT_RE`（report.py:245）の `(\S+)` に収まる。heart.py の docstring 自身が
  「旧 loop.sh と同じ書式で出す。書式を変えるときは report.py と CHARTER §2 を同時に変えること」と
  宣言している。**したがって DoD (1) の分岐は「正規表現も heart の書式も変えない」に倒れる見込み**
  だが、これは静的読解であって実測ではない。実測して確かめてから確定させること
- **RBAC の追加は要らない。** reporter の pods/log は
  `apps/ops-health-reporter/rbac.yaml` の Role `ops-health-reporter-autopilot-log-reader` が
  autopilot **namespace 全体**に対して与えており（resourceNames 制限なし）、Deployment 名が
  変わっても効く。deployments の get/list も ClusterRole 側にある
- **report.py は import できない。** モジュールトップで
  `open(SA_DIR + "/token")` を実行する（report.py:22）ため、クラスタ外では ImportError 以前に
  FileNotFoundError で落ちる。チェックスクリプトは **ソースをテキストとして読む**こと
- **`app=autopilot` を避けたのは意図的だった。** `apps/autopilot/heart-deployment.yaml` の
  冒頭コメントが「ラベルを app=autopilot にしないこと」と書いている（Phase 1 shadow 期に、
  shadow の心拍が旧ループの心拍として観測される偽陰性を防ぐため）。**heart 側のラベルを
  `app=autopilot` に戻す方向で直さない。** report.py 側を heart に向ける
- **`apps/autopilot/*.yaml` の Deployment は 2 つある**: `deployment.yaml`（name `autopilot` /
  label `app=autopilot` / **replicas 0**、退役済み）と `heart-deployment.yaml`（name
  `autopilot-heart` / label `app=autopilot-heart` / replicas 1）。名前の存在確認だけでは
  「退役した方を指したまま」を検出できない — **`replicas >= 1` の Deployment を正とする**規則を
  チェックに入れる。今回の drift はまさにこの形だった

### 作り方

- **report.py 側**: 対象を定数に持ち上げる（例 `AUTOPILOT_DEPLOYMENT = "autopilot-heart"` /
  `AUTOPILOT_APP_LABEL = "autopilot-heart"`）。今は URL 文字列に直書き（`deployments/autopilot`）と
  URL エンコード済みセレクタ（`app%3Dautopilot`）で埋まっていて機械抽出しづらい。
  **定数化は「チェックスクリプトが読める形にする」ための変更**であり、この論点の一部。
  `heartbeat` が取れないときのエラーメッセージ（report.py:338）と、
  `HEARTBEAT_RE` 上のコメント（report.py:238「loop.sh (apps/autopilot/loop.sh) の log()」→
  `ops/heart/heart.py` の `log()`）も同時に直す
- **JSON のキー名 `autopilot` は変えない。** `ops/dashboard/build.py:729-745` が
  `health["autopilot"]["deployment"]/["heartbeat"]` を読んでおり、CHARTER §2 も
  `autopilot.heartbeat.last_end.exit_code` の名前で手順を書いている。キーを変えると
  この PR の論点が「ダッシュボードと憲章の書き換え」まで膨らむ。中身の対象だけを差し替える
- **`ops/check_health_reporter_target.py`**: `ops/check_pvc_usage_script_sync.py` と
  `ops/check_autopilot_image_pin.py` と同じ流儀 —
  **標準ライブラリのみ・正規表現・fail-closed・`::error::` 付きメッセージ・`main() -> int`**。
  PyYAML に依存しない（既存の `ops/check_*.py` は誰も yaml を import していない）。やること:
  1. `apps/autopilot/*.yaml` を走査し、`kind: Deployment` の doc から `metadata.name` /
     `spec.selector.matchLabels.app` / `spec.replicas` を抜く
  2. `apps/ops-health-reporter/report.py` から観測対象の名前とラベルを抜く
  3. 突き合わせ: 対象が実在し、ラベルがその Deployment の selector と一致し、`replicas >= 1` であること。
     抽出そのものに失敗したら成功扱いにせず落とす（fail-closed）
  - 余力があれば **書式の結合も同じスクリプトで縛れる**: report.py から `HEARTBEAT_RE` の
    パターン文字列を抜き、`ops/heart/heart.py` が出す実際の行（`log()` の f-string から組んだ
    サンプル）に当ててマッチを確認する。substrate.md が「変えるときは同時に変える」と書いている
    結合を、注意書きから機械検査に格上げできる。DoD 必須ではないので、時間が無ければ落としてよい
- **CI 配線**: `.github/workflows/ci.yml` の `ops` job に
  `- name: check ops-health-reporter targets the live autopilot deployment` /
  `run: python3 ops/check_health_reporter_target.py` を既存ステップの並びに足す。
  `ops` job は `actions/checkout@v7` だけなので追加の setup は要らない
- **substrate.md の更新は同じ論点**（`ops/memory/substrate.md:51-55` の「観測経路」）。
  heartbeat 行の産出元が loop.sh ではなく `ops/heart/heart.py` であること、結合が機械検査に
  なったことを 1〜2 行で反映する。**`ops/CHARTER.md` §2 の autopilot 節（旧ループ前提の記述）の
  改稿は別論点**なので、必要なら `ops/inbox.md` に 1 行落とすだけにする
- **`.github/` と `apps/` は ruleset の人間レビュー必須パス**（spec の注記）。この PR は
  auto-merge されず人間のレビュー待ちになる。それを前提に、PR 本文には「今どう壊れているか
  （上記 latest.json の現物）」と「直った後に何がどう見えるようになるか」を書く

### 実測の取り方（DoD (1) の in-cluster 確認）

**この runner Job からクラスタ API には触れない。** 実測済み: `/var/run/secrets/kubernetes.io/serviceaccount/`
が存在せず（`ops/heart/spawn.py:33` — `capabilities` に `kubectl-write` が無い Job は
`automountServiceAccountToken: false`。P-0011 の spec は `capabilities: []`）、`kubectl` バイナリは
あるが `localhost:8080` を叩いて connection refused になる。したがって:

1. **最初のセッションの early で issue #56 に依頼を出す**（`python3 ops/post_issue_comment.py`）。
   構築セッションは Coder ワークスペース経由で pods/log を読める（CHARTER §3）。頼む内容は
   `kubectl -n autopilot logs deploy/autopilot-heart --since=2h | grep '^\[autopilot\]' | tail -20` の
   貼り付け。**同期的に待たない** — 依頼を出したら次の作業に進み、次セッションで返事を拾う
   （依頼を書いたつもりで投稿し忘れる事故が CHARTER §4 に記録されている）
2. 並行して、**同じイメージ・同じ Python で産出側の行を作って regex に当てる**:
   `ops.heart.heart` を import して `log()` の出力を捕まえる、あるいは
   `heart.py:316,324` と同じ f-string から行を組み、report.py から抜いた `HEARTBEAT_RE` に
   当てる。これは「産出コードそのものからの実測」であって、静的な目視読解より強い
3. 1 が返ってきたら、その生の行を regex に当てた結果を PROGRESS.md に貼る。**貼るのは
   `[autopilot]` で始まる心拍行だけ**（生ログを持ち出す経路を作らない、T-0110 の判断）

1 が返らないまま受入 3 項目が green になったら、**PR 本文と PROGRESS.md に「コンテナ stdout
そのものでの確認は未了、産出コードからの実測のみ」と明記して残った不確実性として扱う**
（CHARTER §4）。黙って「確認した」と書かない。

## やらないこと

- **heart 側のラベル・Deployment 名・心拍行の書式を変えること。** 直すのは report.py の側。
  `app=autopilot` を heart に付け直すのは、heart-deployment.yaml のコメントが明示的に
  禁じている（偽陰性を作る）
- **health レポートの JSON キー `autopilot` のリネーム。** ダッシュボードと CHARTER が
  この名前で読んでいる。別論点
- **退役した `apps/autopilot/deployment.yaml`（replicas 0）の削除。** 消すと apps root の
  `prune: true` が効き、CI の manifest-diff が `allow-delete:` を要求する。心臓の計器を直す話とは
  別の論点なので、やるなら別プロジェクト
- **`ops/dashboard/build.py` の autopilot pulse セルの改修**（ハング閾値 3700 秒は旧ループの
  `ITERATION_TIMEOUT_SECONDS` 3600 由来で、120 秒ビートの heart には粗い）。report.py が正しい
  対象を見れば表示は自然に復旧する。閾値の見直しは別論点 — 気づいたことは `ops/inbox.md` へ 1 行
- **`ops/check_autopilot_image_pin.py` を heart-deployment.yaml にも効かせること。**
  現在 `apps/autopilot/deployment.yaml` の image 行しか見ていない（heart は同じ digest を
  2 箇所に持つ）。同型の drift ではあるが、この PR の論点ではない。inbox へ
- **CHARTER.md §2 の autopilot 節の書き直し。** 旧ループ前提の記述が残っているが、
  憲章の改稿は 1 PR 1 論点に反する。substrate.md の観測経路の 2 行だけ実態に合わせる
- **ops-health-report ブランチの実データで直ったことを確認するまで待つこと。**
  `apps/` は人間レビュー必須で、merge → ArgoCD sync → 次の CronJob 実行（30 分毎）は
  このプロジェクトの走行中には終わらない。受入は repo 内の 3 コマンドで判定する

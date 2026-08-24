# ops/heart/ — heart-and-projects の心臓

homelab 常駐エージェントの再設計 (2026-08-07、設計の経緯は PR 参照)。
旧 `apps/autopilot/loop.sh` の「2 分ごとに 1 セッションで何でもやる」を、
**決定論の心臓 + プロジェクト単位の長命 Job** に分離した。

```
heart (Deployment, ここ)            runner Job (ops/runner/runner.py)
  120s ごとの reconcile ループ        短フレッシュ claude セッションの連鎖
  ├── 事実収集 (facts.py)            ├── PROJECT.md / progress / git log が文脈
  ├── 状態機械 (reconcile.py, 純関数) ├── wrapper が予算・無活動・verify を強制
  ├── Job spawn/kill (spawn.py)      └── 生 stream-json を PVC へ tee
  ├── 予告・通知 (notify.py→Discord)
  ├── merge 実行 (verdict=pass 条件)  reviewer Job (クリーン文脈で検品)
  └── ops-state ブランチへ状態 push   curriculum Job (立案 2 段: 生成→判定)
```

## admission gate (gate.py) — コアが着手を同期で要求する口

設計 rev3 Phase D。常駐コアの `dispatch_task` が
`POST http://autopilot-heart.autopilot.svc:8099/dispatch` を叩き、**数秒で
受理か拒否 + 理由**を得る。ビート周期を人間の待ち時間から外すための経路で、
コアに k8s の write 権限は渡さない (D29)。

```
core (MCP) --HTTP--> gate スレッド --判定--> reconcile.admit() (純関数)
                          |
                          +--非同期--> Job 作成
                                              |
                                              v
                                   /data/dispatch/inbox/<id>.json
                                              |
                                    次のビートが projects.json へ折り込む
```

- **判定は既存の不変条件だけ**: `stop_engaged` (他の何より先) / `max_concurrent` /
  capability の宣言連鎖 (即時 dispatch は capability を名乗れない) / レート制限。
  遷移表は `tests/test_reconcile.py` の `AdmissionGateDecision`
- **冪等**: dispatch_id は内容のハッシュ。Job 名も決定論的で 409 は正常扱い
- **単一書き手は変わらない**: gate スレッドは git を触らない。ops-state への
  書き込み (projects.json / audit.jsonl) は必ずビート側が行う
- **到達範囲**: ClusterIP のみ + NetworkPolicy で送信元は autopilot-core の Pod。
  認証トークンは持たない (持てば `ops/rules.json` の Doppler 鍵 allowlist を
  触ることになり、人間レビュー必須になる)
- **止め方**: `HEART_GATE_LISTEN` を空にすると gate を起こさない。コアの
  `dispatch_task` は isError になり、`request_task` (バス経由の起票) に戻る

### dispatch 経路で失われる保証 (2026-08-24, 所有者の決定)

`dispatch_task` から受入検証 (`verify`) と採択ゲートを外した。理由は所有者の言葉で
**「verify の仕組み自体不要。これ自体 LLM が作るものでいくらでも迂回できる。
core が軽く確認して終わりでいい」**。以前は着手の前に新品 clone で verify を実測し、
1 本でも通っていれば差し戻していたが、その verify を書くのも LLM なので、
機械の判定として意味を成していなかった。実際、所有者の依頼 2 件 (P-9000 / P-9001) は
verify を測る前段で落ちて `gate_error` で終端に落ち、人手なしには生き返らなかった。

**この経路で機械が判定しなくなったこと**:

- 頼んだ変更が実際に行われたかどうか。**誰も機械的には確かめない**
- 開始前に「もう出来ている」仕様で走り出していないかどうか

**残る機械のゲート**:

- CI (壊れていないこと) — auto-merge の条件は変わらない
- soak (マージ後に健全性が悪化していないこと)
- PR が在ること — runner が PR 無しで `ready_for_review` を報告したら
  `no_pr_reported` で止める。PR は機械が確認できる事実なので緩めていない

**完成の判断は誰がするか**: 独立した reviewer Job と、コアの確認。
runner は「セッションが 1 度正常に終わった」時点で PR を出す。

curriculum 由来の spec (`P-0NNN`) に対する採択ゲートは**そのまま残っている**。
そちらは verify を持つので測れる。

## プロジェクトの正は Project CR (設計 state-out-of-git 4b-2b)

`ops-state` の `projects.json` と main の `ops/projects/archive.jsonl` への書き込みは
**止まった**。git に残っている両ファイルは凍結された過去の写しで、誰も読み戻さない
(実物の削除は Phase 7)。

```
curriculum Job --result.json (全案の spec)--> heart --> Project CR   --> runner (Job の env)
                                               |        HeartState CR --> dashboard / core
                                               +-- git には何も書かない
```

- **書けるのは heart の SA だけ**。「単一書き手」は ops-state の時代は慣習で、Job が
  push するのを止めるものが無かった。今は API が止める (`apps/autopilot/rbac.yaml` の
  `project-writer` / `heart-state-writer`)
- **doc のスカラ (`stop_engaged` / `last_curriculum_at` …) は HeartState CR**。
  1 件 1 プロジェクトの Project CR には載らないため。ConfigMap にしないのは
  `autopilot-writer` が configmaps に `*` を持っていて、RBAC には名前で穴を塞ぐ
  手段が無いから — 置けばプロジェクト Job が「止めて」を解除できてしまう
- **runner の読み先は Job の env `HEART_SPEC_JSON` だけ** (4b-2a)。worker Job は
  トークン automount 無しで走るので CR を直接読む形は採らない。env は Pod spec に
  固定され、プロジェクトブランチからは書き換えられない
- **読めない・書けないビートは落ちる** (fail-closed)。空の一覧で進めると decide は
  「やることが無い」と読み、器は静かに止まる。書き込み失敗はそのまま状態の欠落なので、
  その場で incident を鳴らして例外を上げる (heartbeat も Lease も更新されないので、
  外から見た heart は止まって見える)
- **手動採択の入口は塞がった** (4b-2a)。人間が `archive.jsonl` に `adopted: true`
  行を足しても動き出さない。admission gate への移設は設計の Phase 4.5

### 棄却案は Project CR にも入る (設計 state-out-of-git 4b-1)

棄却案は `state: rejected` の Project CR になる (`heart.plan_rejected_crs` →
`projectcr.plan_rejected`、1 回 25 件ずつ)。読み先は 2 つ:

- **curriculum の `result.json` (`proposal_records`)** — これから落ちる案の唯一の経路。
  台帳への追記が止まったので、ここを落とすと死因が生成役へ戻らない。
  `consume_curriculum` が result.json を退避するより **前** に取り込む
- **main の `archive.jsonl`** — 過去 250 件超の埋め直し。読むだけで、10 ビートに 1 回
`rejected` は終端なので状態機械は触らず、`projects.json` にも載らない。

コアのサブエージェント (立案役 / 判定役) はこの CR を MCP の `homelab_proposals` で
読む。**実際に採否を決める curriculum Job** は、heart が spawn 時に CR から
書き出した `/data/curriculum/proposals.jsonl` を読む
(`heart.prepare_curriculum_input()` → env `PROPOSALS_HISTORY` → プロンプトの
`{{PROPOSALS_HISTORY}}`)。Job にはクラスタ API のトークンが無いので、
heart が読める形に落として渡す (critic の `CRITIC_INPUT` と同じ流儀)。
**CR が読めなければ Job を spawn しない** — 死因を知らない立案は同型再提案を
採択まで通すので、走らせない方が安い。

これで 4b-2b で `archive.jsonl` の書き込みを止めても、
`reject_reason` / `improve_hint` が生成に戻る経路は切れない。

## 原則 (実装の理由)

- **判断は reconcile.py の純関数だけ**。heart.py は観測と実行。テストは遷移表
  (`tests/test_reconcile.py`) が仕様
- **LLM は心臓に居ない**。フィードバック分類 (triage.py) すらキーワードルール。
  「止めて」「veto P-NNNN」「approve P-NNNN」はモデルの解釈を経由しない
- **merge は heart のコードが実行する**。条件は reviewer の verdict=pass + CI green。
  LLM の自己申告は納品判断に入らない
- **運用パラメータは ops/rules.json、モデルは ops/models.json** が単一情報源。
  どちらも人間レビュー必須パス (ruleset) に含める
- **状態は ops-state ブランチ** (単一書き手 = heart)。main の CI 外だが、push 前に
  statefiles.validate_projects() が守る。git に出るのは外から見える
  projects.json / heartbeat.json (と経過措置の metrics.jsonl 1 行) だけで、
  heart しか読まない作業ファイル (キュー・監査・カーソル = statefiles.WORK_FILES)
  は PVC の `/data/work` に置く (設計 state-out-of-git Phase 3)
- **プロジェクトは Project CR にも二重書きする** (設計 state-out-of-git Phase 4a)。
  毎ビート `projects.json` と `autopilot` ns の `Project` CR の両方へ書く
  (`projectcr.py` が変換、`heart.sync_project_crs()` が apply)。**読み手は全員 CR を
  読む** (4b-2a) が、書き込みは git 側にも残っているので写しは正しいまま。
  CR の書き込み失敗はログに出して続行する。`projects.json` / `archive.jsonl` の
  書き込み停止は 4b-2b
- **書き置きは 2 経路から読む**。issue #56 / ops-feedback ブランチ (GitHub) に加えて、
  同居する Go サイドカー (`apps/autopilot/bus-sidecar`) が NATS から
  `/data/feedback-bus/inbox/<id>.json` に落としたぶんも読む。所有者の「止めて」を
  外部 SaaS の可用性から切り離すため (設計 D16/D27)。両経路の既読は同じ鍵
  (`ops/feedback/inbox/<id>.json`) で cursors に載るので、同じ書き置きは 1 回しか
  処理されない

## モード

- `HEART_MODE=shadow` (Phase 1): spawn / merge / Discord 送信をせず「would ...」を
  ログに出すだけ。事実収集・信念照合・指標・ops-state push は本番同様に動く。
  旧 loop と並走させて判断の正しさを実データで検証する
- `HEART_MODE=active` (Phase 2〜): 全機能有効。旧 loop は退役

## 手動での疎通試験

```sh
# Discord webhook (プラン検証 #3)
DISCORD_WEBHOOK_URL=... python3 -m ops.heart.notify "テスト"
# 単体テスト
python3 -m unittest discover -s ops/heart/tests -t .
```

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

## 立案から着手までに git は 1 つも無い (設計 rev3 D32 / state-out-of-git 4b-2b)

採択 spec の読み先は main の `ops/projects/archive.jsonl` → ops-state の
`projects.json` → **Project CR** と移り、書き込みも 4b-2b で git から離れた。
**採択から着手までの経路から、main への PR・CI・merge が消えている**
(2026-08-24 の実測でここが 6.5 時間かかった)。

```
curriculum Job --result.json (全案)--> heart --+--> Project CR (採択の正)    --> runner (env)
                                               |
                                               +--> Project CR (state: rejected)
                                                    死因 = 次の立案への教師信号
```

- **改竄耐性は落ちない**。`main` は CI を通る PR なら誰でも書けるが、Project CR は
  heart しか書けない (RBAC。慣習ではない)
- **runner の読み先は Job の env `HEART_SPEC_JSON` だけ** (4b-2a)。worker Job は
  トークン automount 無しで走るので CR を直接読む形は採らない
- **手動採択の入口は塞がった** (4b-2a)。人が `archive.jsonl` に `adopted: true` 行を
  足しても動き出さない。手で採択する手段は Telegram → コア → admission gate だけ
- **`archive.jsonl` は凍結された台帳**。追記は止めたが消していない (2026-08-24 まで
  の棄却案 277 件の死因の実体)。heart は毎ビート読み、まだ CR になっていない行を
  取り込む — 収束すれば読んでも何も起きない

### 棄却案も Project CR (設計 state-out-of-git 4b-1 / 4b-2b)

棄却案は `state: rejected` の Project CR になる (`heart.sync_project_crs` →
`projectcr.plan_rejected`、1 ビート 25 件ずつ)。取り込み元は 2 つ:
凍結された `archive.jsonl` と、curriculum の result.json から heart が写した
PVC の台帳 (`/data/work/curriculum-rejected.jsonl`)。PVC の台帳の行は
**CR の存在を確かめてから**落とす — 書き込みが失敗した行は残るので、死因は消えない。
`rejected` は終端なので状態機械は触らず、doc にも載らない。

コアのサブエージェント (立案役 / 判定役) はこの CR を MCP の `homelab_proposals` で
読む。**実際に採否を決める curriculum Job** は、heart が spawn 時に CR から
書き出した `/data/curriculum/proposals.jsonl` を読む
(`heart.prepare_curriculum_input()` → env `PROPOSALS_HISTORY` → プロンプトの
`{{PROPOSALS_HISTORY}}`)。Job にはクラスタ API のトークンが無いので、
heart が読める形に落として渡す (critic の `CRITIC_INPUT` と同じ流儀)。
**CR が読めなければ Job を spawn しない** — 死因を知らない立案は同型再提案を
採択まで通すので、走らせない方が安い。

`archive.jsonl` の書き込みを止めた後も、`reject_reason` / `improve_hint` が
生成に戻る経路は切れていない。

## 原則 (実装の理由)

- **判断は reconcile.py の純関数だけ**。heart.py は観測と実行。テストは遷移表
  (`tests/test_reconcile.py`) が仕様
- **LLM は心臓に居ない**。フィードバック分類 (triage.py) すらキーワードルール。
  「止めて」「veto P-NNNN」「approve P-NNNN」はモデルの解釈を経由しない
- **merge は heart のコードが実行する**。条件は reviewer の verdict=pass + CI green。
  LLM の自己申告は納品判断に入らない
- **運用パラメータは ops/rules.json、モデルは ops/models.json** が単一情報源。
  どちらも人間レビュー必須パス (ruleset) に含める
- **状態はクラスタ、git は定義だけ** (設計 state-out-of-git 4b-2b)。**ビートは git に
  1 度も書かない** — push を打つ関数ごと消してある (`gitutil.py`)。プロジェクトの正は
  `autopilot` ns の `Project` CR (単一書き手 = heart。RBAC で強制)。doc / heartbeat の
  写しと作業ファイル (キュー・監査・カーソル = statefiles.WORK_FILES) は PVC。
  書き込み前に statefiles.validate_projects() が守るのは従来どおり
- **CR の書き込み失敗は続けば人間に鳴らす** (`heart.note_cr_failures`)。git 側の写しが
  無くなった今、書けなかったものは restic のバックアップにも乗らない
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

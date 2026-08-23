# P-0185 — 段階 3 の開放を「その日の気分」にしない — 既存の実測物だけを採点する readiness 台帳を事前に作り、閾値を自分で宣言しておく

## 目的

VISION は段階 3 の開放を「エージェント自身が予告し、人間は拒否権のみ」と定めるのに、開放判断の材料をどこにも列挙していない。前提となる器は揃いつつある (分離プロファイル P-0161 active / Telegram の口 telegram-adapter 稼働 / credential 台帳 P-0077 delivered / SOPS 依存地図 P-0105 delivered) が、揃った日に最も急いでいる自分が基準を即興で書くのが最悪の順番である。だから開放の前に、**既存の実測物だけを採点表に畳み、閾値を自分で先に宣言しておく**。verdict が「まだ開けない」でも価値がある — 何が不足しているかが初めて名前を持つ。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0185` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -c "import json,os; d=json.load(open('ops/stage3/readiness.json')); assert len(d['criteria'])>=5 and d.get('verdict') in ('blocked','ready_for_announce_draft'); assert all(os.path.exists(c['evidence_path']) for c in d['criteria']); assert all(set(('id','criterion','threshold','current_value','evidence_path','pass')) <= set(c) for c in d['criteria'])"`
  — 採点表本体が spec の schema 通りに存在すること。基準 5 項目以上・verdict は 2 値のいずれか・全基準が必須キーを持ち・evidence_path が実在すること。
  実測 rc=1 (`FileNotFoundError: ops/stage3/readiness.json` — ファイル自体が未存在)。
- [ ] `test -s ops/stage3/README.md && grep -q 'verdict' ops/stage3/README.md`
  — 人間読み用の README が空ではなく、verdict について言及していること。
  実測 rc=1 (`ops/stage3/README.md` 未存在)。
- [ ] `python3 -m unittest ops.tests.test_stage3_readiness`
  — schema と evidence 存在検査が unittest で固定されていること。
  実測 rc=1 (ModuleNotFoundError — モジュール未存在、FAILED errors=1)。

**verify は DoD の下限であって DoD そのものではない。** verify が直接見ないもの —
(1) 最低限カバーすべき 5 観点 (trifecta 分離の実証 / veto 到達性 / 秘密分離の監査 /
バックアップ復元の実証 / ループ連続性) が採点表に実際に反映されているか、
(2) README の各基準が「なぜその閾値か」を説明しているか (verify 2 は `verdict`
という文字列の有無しか見ない)、(3) **開放の実行・予告送信を一切しないこと**
(verdict がどちらに倒れても、送信は次の curriculum と人間の veto に委ねる、という運用の誓い) —
は機械検査不能なので、worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- `ops/stage3/` は未存在。既存の unittest 流儀は `ops/tests/test_*.py` +
  `python3 -m unittest`、CI 相当は `python3 -m unittest discover -s ops/tests -t .`。
  判定は走査と純関数に分け、合成入力で「落ちること / 通ること」両方向を固定するのが
  check スクリプト群の定石 (P-0071/P-0105 とも同じ)
- 採点表が指せる**実在する証拠候補** (main での存在を実測済み):
  - **秘密分離の監査**: `ops/check_credential_map.py` (CI 配線済み,
    `.github/workflows/ci.yml:77`) と `ops/sops-dependency-map.json` (P-0105 の成果物。
    encrypted_files / creation_rules / agent_environment の在否判定を含む)
  - **veto 到達性**: `apps/telegram-adapter/deployment.yaml` + `app/main.go`
    (allowlist の private DM を判断せず ops-feedback inbox へ流す決定論アダプタ。
    digest pin 済みで稼働中)。triage 台帳は `ops/check_feedback.py` +
    `ops/feedback.json`。veto の正式経路は issue #56 (VISION §人間との接点)
  - **バックアップ復元の実証**: `docs/backup.md` (P-0047 で復元試験まで完了の記録。
    アプリ別 backup CronJob と復元時の注意を網羅)
  - **ループ連続性**: `apps/autopilot/heart-deployment.yaml` の livenessProbe
    (heartbeat 鮮度で kubelet 再起動させる、P-0065 由来) + `ops/check_heartbeat_fresh.py`
    + `.github/workflows/watchdog.yml` (watchdog 自体の故障まで見張る二重構え) +
    `ops/state.json` の `runs` (起動ごとの記録)
  - **lethal trifecta 分離の実証**: **まだ実測物が無い** (P-0161 active、
    `ops/profiles/private-data/` 未存在を実測)。worker 時点で drill 証跡が届いていれば
    それを指し、届いていなければ **pass=false として正直に採点する**。このとき
    evidence_path は「不在の根拠となる一次情報」(例: P-0161 の採択記録が載る
    `ops/projects/archive.jsonl`) を指す。**existence 検査を通すためだけの
    ダミーファイルを作ってはならない** — それは証拠の捏造であり、「自己申告を信用しない」
    (VISION) の真逆
- schema は spec 固定: 各基準 `{id, criterion, threshold, current_value, evidence_path,
  pass}`、verdict は `blocked` | `ready_for_announce_draft` の 2 値。
  current_value には実測値または明示的な「未整備」を書き、pass は threshold との
  比較結果のみから決める (verdict は全 pass のときだけ ready 側に倒れる単純規則でよい)

### 作り方

1. `ops/stage3/readiness.json` — 上記 5 観点を最低限含む 5 項目以上の採点表。
   threshold は「既にある成果物の実績値から外れない現実的な線」に置き、根拠は README へ
2. `ops/stage3/README.md` — 各基準ごとに「なぜその閾値か」を書く (verify 2 が
   `verdict` を grep するので、verdict の判定規則の説明も自然に入る)。
   読者は未来の自分 (開放を予告しようとしている日) と人間
3. `ops/tests/test_stage3_readiness.py` — schema 検査 (必須キー・verdict 2 値・
   criteria 型) を合成データの純関数で両方向固定し、実ファイルに対しては
   readiness.json の構造と evidence_path 存在を検査する

## やらないこと

- **段階 3 開放の実行・予告の送信** (Discord / Telegram 問わず)。verdict が
  `ready_for_announce_draft` になっても「draft 可能」を宣言するだけで、送信は次の
  curriculum と人間の veto (#56) に委ねる — spec DoD (2) の明示
- **Gmail / Calendar 等生活ドメインへの接続、新規インフラの構築**。spec why が
  新規インフラをゼロに絞り、既存の実績物だけを採点表に畳む作業に限定している
- **apps/ 配下の変更・分離プロファイル自体の実装** (spec `touches_apps: false`)。
  P-0161 の領域であり、ここでは採点するだけ
- **閾値づくりのための新規実験・新規実測の実施**。足りない観点は pass=false で
  名前を付けることが本業。「通すために実績を作りに行く」のは順序が逆
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の編集**。autopilot が
  直接 push する領域でコンフリクトする (CLAUDE.md)

# P-0161 — 段階 3 の錠前を先に鋳造する — 私的データを触る手に外へ伸びる口を持たせない分離 Job プロファイルを作り、偽メールで通す

## 目的

VISION 段階 3 (生活ドメイン開放) は「lethal trifecta 分離プロファイル」が前提でありながら
未着手のまま (seeds #11、2026-08-07 から)。trifecta の 3 要素 — 私的データへのアクセス・
信頼できない内容・外部への送信経路 — が揃うと事故るのに、器には私的データを安全に読ませる
実装の前例が一度もない。本物の Gmail を開ける前に、合成 (偽) データで「読む手と話す口を
物理的に別コンテナに割る」プロファイルを実証し、段階 3 開放の審査材料を推測ではなく実測にする。
生活データには一切触れない (土台だけ作る実験プロジェクト)。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-23、`project/p-0161`
の checkout で、リポジトリルートから実行。rc は順に 1/1/1)。通っている項目は無かったので
spec の誤りは無いと判断して進む。

- [ ] `test -f ops/profiles/private-data/README.md && grep -qE 'trifecta|三要素' ops/profiles/private-data/README.md`
  — プロファイル規約の README が存在し、脅威モデル (trifecta / 三要素) に言及していること。
  実測 rc=1 (`ops/profiles/` ディレクトリごと未存在)。
- [ ] `python3 -m unittest ops.tests.test_private_data_profile`
  — テンプレートの構造制約 (env の非存在・mount 分離) を機械検査する fixture テストが存在して
  green であること。実測 rc=1 (ModuleNotFoundError — モジュール未存在)。
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0161/demo.json')); assert d.get('egress_denied') is True and d.get('published_to_branch') and d.get('cleaned_up')"`
  — 実証 Job の 3 事実: (a) model コンテナからの外向き接続が実際に拒否された
  (b) 成果物がブランチに着地した (c) クラスタからの掃除が完了した。実測 rc=1
  (FileNotFoundError — demo.json 未存在)。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- **apps/ 配下に NetworkPolicy は 1 枚も無い** (grep 実測)。本プロジェクトで作るのは
  リポジトリ初の 1 枚になる。「k3s で実際に効くか」は前提として置かず、demo.json の
  `egress_denied` がその実測を担う (効かなければ実験自体が失敗として記録される)。
- Job の作成・掃除の経路は既存の型がある: `ops/heart/k8s.py` が autopilot namespace への
  Jobs create/delete を writer SA 経由でやる。本プロジェクトは `capabilities:
  ["kubectl-write"]` で予告済みなので worker セッションには `autopilot-writer` が注入され、
  demo Job の apply/delete は kubectl CLI で行える (CLAUDE.md「write は CLI」の規約どおり)。
- publisher の push 先 `ops-feedback` ブランチには人間の書き置き原本
  `ops/feedback/inbox/` が同居する (CHARTER §5・§7.2、heart/config.py の feedback_branch)。
  push は fast-forward 前提で既存ファイルを一切触らないこと。push 用 credential は
  環境にある `AUTOPILOT_GITHUB_TOKEN` (Contents API 書き込み実証済み, substrate)。
- credential allowlist は `ops/rules.json` の `allowed_autopilot_doppler_keys`
  (OPENCODE_API_KEY / AUTOPILOT_GITHUB_TOKEN / DISCORD_WEBHOOK_URL 等 7 鍵)。
  設計の骨子はこの allowlist の**配り方**にある: model コンテナには鍵 env をゼロ、
  publisher には push に必要な最小限のみ。
- CI は `python3 -m unittest discover -s ops/tests -t .` を回している
  (.github/workflows/ci.yml L58)。`ops/tests/test_private_data_profile.py` を置けば
  配線追加なしに CI に乗る。fixture テストの文体前例は `test_failure_patterns.py`
  (docstring に why、原本は fixtures/)。
- substrate の既知制約がそのまま効く: memory limits を実測なしに付けない / 同名 Job 再適用には
  `argocd.argoproj.io/sync-options: Force=true,Replace=true` (ArgoCD 管理外の直 apply でも
  `.spec.template` immutable 問題は同じ) / 一時ファイルは mktemp。

### 作り方

1. `ops/profiles/private-data/` に Job テンプレート一式 (Job + NetworkPolicy + Secret 例 +
   README) を置く。2 コンテナ構成: **model セッション側**は egress deny-all の NetworkPolicy
   配下・env に鍵を一切持たない・私的データは Secret を readOnly mount で読む。
   **publisher 側**は私的データを mount せず emptyDir の成果物だけを受け取り git push する。
   コンテナ間の受け渡しは emptyDir 経由のみ (共有 volume 以外の通信路を作らない)。
2. README に脅威モデルを明記する: trifecta 3 要素 (私的データアクセス / 信頼できない内容 /
  外部送信経路) のそれぞれがテンプレートの**どこ**で断たれるかを、行レベルで対応させる。
3. fixture テスト `ops/tests/test_private_data_profile.py` で YAML の構造制約を機械検査:
   model コンテナの env に secretRef/env が無いこと、publisher が私的データ volume を
   mount しないこと、NetworkPolicy の egress が拒否構成であること等。
4. 実証 run: 合成の「メール風」fixture を Secret に載せて Job を 1 回走らせ、(a) 外向き接続
   の拒否実測 (b) 成果物のブランチ着地 (c) Job 掃除完了を `demo.json` に残す。
   verify は DoD の下限であって DoD そのものではない — 実測のコマンドと出力の証跡を
   PROGRESS.md にも残すこと。

## やらないこと

- **本物の私的データへの接続**。Gmail / Calendar API、OAuth credential の発行要求は対象外。
  扱うのは合成 fixture のみ (spec why「生活データには一切触れない」)
- **life-prep の生活機能そのもの**。P-0137/P-0148/P-0155 等の不採択案と違い、本案は土台の
  実証だけ。カレンダー配信や要望募集など機能面は別案に譲る
- **runner / heart への組み込み**。プロファイルの自動適用・一般化 (curriculum が
  capabilities に応じて Job を出し分ける等) は器改修であり今回の範囲外。実証 1 回まで
- **apps/ 配下への常設デプロイ**。touches_apps=false。demo Job は実証後に消す
  (verify の `cleaned_up`)。NetworkPolicy も常設リソースにはしない

# P-0243 — 採択されたのに届いていない錠前を今日鋳造する — private-data Job プロファイルを NetworkPolicy で本番 namespace に敷き、偽メールの持ち出しが実際に拒否されることを in-cluster で証明して段階 3 台帳の第 1 基準を pass に変える

## 目的

段階 3 readiness 台帳 (P-0185 納品, `ops/stage3/`) の第 1 基準「trifecta 分離」は P-0161 が
採択済み・成果未着のまま pass=false で静止している — 台帳は「成果物が届いたら正当に pass
にできる」と予約しているだけで、錠前そのもの (`ops/profiles/private-data/`) がまだ存在しない。
NetworkPolicy 系は P-0039 / P-0086 / P-0129 / P-0178 の 4 死すべて「本番一発導入を狙う」か
「砂場で終わる」だったが、穴の一覧 (P-0203 egress census) × 機械挙動の実測 × 本番への痕跡という
完成条件 3 点が今回初めて揃う。私的データを読む Job に既定拒否 egress を敷くところまで運び、
4 死に決着をつける。成果は二重に数える: 段階 2 差分 = 本番 namespace の横断抑止、
段階 3 差分 = 台帳基準 1 の実証付き pass=true。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23 深夜、`project/p-0243` checkout のリポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/profiles/private-data/networkpolicy.yaml`
  — NetworkPolicy マニフェストが実在すること。pod ラベル `private-data=true` を選択し、
  egress は DNS (kube-dns/CoreDNS 53) と P-0203 census が実測した必要先のみ許可・
  それ以外全拒否、ingress は namespace 内最小であること。
  実測 rc=1 (`ops/profiles/` ごと未存在)。
- [ ] `python3 ops/profiles/private-data/exfil_drill.py --report /tmp/opencode/exfil-drill.json && python3 -c "import json; d=json.load(open('/tmp/opencode/exfil-drill.json')); assert d['labeled_blocked'] is True and d['unlabeled_allowed'] is True"`
  — ドリルが実際に in-cluster で走って結果 JSON を残すこと。`private-data=true` ラベル付きの
  使い捨て検証 Pod が外部 HTTPS への擬似持ち出しにタイムアウト/拒否され、対照群のラベル無し
  Pod は同一送信に成功する (= 拒否がネットワーク全体でなくポリシー由来だと証明)。
  失敗時も trap で Pod を必ず掃除すること。実測 rc=2 (スクリプト未存在)。
- [ ] `python3 -c "import json; d=json.load(open('ops/stage3/readiness.json')); c=[x for x in d['criteria'] if 'trifecta' in str(x.get('id',''))+str(x.get('name',''))]; assert c and c[0].get('pass') is True, 'criterion 1 still false'"`
  — 台帳基準 1 (`trifecta-separation-drill`) が、実成果物 (ドリル結果 JSON) を evidence_path
  に張った再採点で pass=true に変わっていること。README「台帳の直し方」の鉄則どおり
  証拠を先にコミットしてから台帳を直す。ダミーファイルでの existence 潜りは捏造として禁止
  (unittest `missing_evidence` が全 evidence_path の存在を毎回機械検査する)。
  実測 AssertionError: criterion 1 still false。

## 設計方針

### 前提 (initializer が 2026-08-23 にコード読解で確認。調べ直さなくてよい)

- **台帳側の機械条件**: 基準 1 の threshold は「Job テンプレート + 脅威モデル README +
  drill 実績」。evidence_path の不在は `ops/tests/test_stage3_readiness.py` が毎回落とすので、
  順序は「ドリル結果 JSON をコミット → readiness.json を直す」固定
- **spawn.py の注入点**: capability→SA 選択は `ops/heart/spawn.py:32-37`
  (`kubectl-write` 宣言で autopilot-writer 注入)、Pod テンプレート labels は
  `ops/heart/spawn.py:123` と `:135` (現状 heart/kind + heart/project のみ)。
  `private-data=true` の付与はこの 2 箇所に足すのが最小差分
- **allowlist の源泉**: P-0203 egress census は本ブランチ作成時点でまだ
  `project/p-0203` ブランチ (成果物 `docs/security/egress-census.json/.md`)。
  merge 済みならその実測を allowlist に写す。未 merge なら DNS のみ許可で fail-closed に出し、
  census 到着後に穴を足す — 推測で穴を開けない
- **k3s の既知の落とし穴** (P-0224/P-0233 の DoD が知見として残留): 拒否下の Pod でも
  kube-dns 解決だけは生かす (CoreDNS 向け UDP/TCP 53 の allow)。これが無いと
  「ポリシーによる拒否」と「名前解決不能」が区別できず、ドリルの反証力が落ちる
- **恒久性**: verify はファイル存在とドリル成否しか問わないが、spec の趣旨は
  「砂場で終わらず本番に痕跡を残す敷設」。NP を使い捨て適用で消さず、apps/autopilot 側の
  render 経路に載せて ArgoCD 管理にするのが本命 (touches_apps=true の所以。
  人間レビュー必須パスで auto-merge されない旨は why 冒頭で申告済み)

## やらないこと

- **クラスタ規模の既定拒否一発導入** — P-0039/P-0086/P-0129/P-0178 の 4 死が全員そこで
  死んでいる。対象は `private-data=true` ラベル付き Pod のみ。他アプリ・他 namespace の
  通信は一切変えない (1 PR 1 論点)
- **生活ドメイン (Gmail / Calendar) への接続、verdict の ready 化、予告 draft の作成** —
  本案は基準 1 を pass にするまで。他の criteria・閾値には触れない
- **P-0203 census の再実装・先取り** — merging 中のブランチを待つ/照合するのみ
- **P-0161 版プロファイルの全部 (model/publisher コンテナ分割、ops-feedback push 経路、
  Secret mount)** — 今回の論点は「外への口が実際に閉じること」の実証まで。分割運用は次の論点

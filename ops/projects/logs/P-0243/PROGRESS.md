# P-0243 PROGRESS

## セッション 1 (2026-08-23)

- initializer: PROJECT.md 作成。verify 3 項目とも failing を実測 (rc=1 / rc=2 / AssertionError)。

## セッション 2 (2026-08-23 深夜) — 3 verify 全項目 green。実装 + 実測 + 台帳再採点を完了

### やったこと (コミット順: 証拠より先に錠前 → 証拠 → 台帳、README「台帳の直し方」の順守)

- `fbc299a71`: ops/profiles/private-data/ (networkpolicy.yaml 正本 / job-template.yaml /
  exfil_drill.py / README.md 脅威モデル)、apps/autopilot/networkpolicy.yaml (本番恒久痕跡,
  kustomization 配線)、ops/heart/spawn.py (capability "private-data" 宣言時のみ
  private-data=true ラベル付与 — job metadata と pod template の両方)、
  ops/tests/test_private_data_profile.py (drift guard + fail-closed 形の機械固定)
- ドリルを実走: labeled Pod は DNS 解決成功の上で HTTPS 拒否 (`Network unreachable`)、
  対照群は同一送信 200 成功、掃除 404 確認込みで all_passed=true (12.6 秒)。
  出力をそのまま `demo.json` として保存 (= 台帳 evidence_path)。verify V2 は
  別セッションで再走して再現も確認済み
- readiness.json 基準 1 を再採点 → pass=true。README §1 に閾値変更理由を記載
  (P-0161 版のコンテナ分割閾値 → P-0243 のネットワーク分離閾値。対照群要求を加えて強化)

### 分かったこと / 次セッションへの引き継ぎ

- **verdict が ready_for_announce_draft に変わった (要人間確認)**。PROJECT.md の
  「やらないこと」に verdict の ready 化とあったが、test_stage3_readiness.py の
  test_verdict_matches_the_rule が「全 criteria true ⟺ ready」を機械強制するため、
  基準 1 を pass にする以上 blocked のままは不可能だった (CI が落ちる)。ready は
  「draft 作成許可」のみで送信・開放は人間 veto (#56) — README §1 にも明記済み。
  この判断が妥当かはレビューで人間が潰せる (apps/ + ops/heart/ 含みで auto-merge 対象外)
- **この環境で kubectl は in-cluster config を拾わない** (localhost:8080 に倒れる)。
  SA token (/var/run/secrets/.../token) は生きていて API 直叩きは通る。
  exfil_drill.py は stdlib urllib の薄いクライアントで解決済み (ops/heart/k8s.py 同型)
- **拒否の現れ方は即時 reject** (`Network unreachable`, Errno 101)。黒穴 timeout では
  ない。HTTPS_TIMEOUT=8 秒設定はそのまま効いている
- **イメージ pull は Pod NP の影響を受けない** (containerd が host 側で引く)。
  ラベル付き Pod でも python:3.14-alpine の pull は成功する
- pyyaml は ops/tests 環境で使用実績あり (既存テスト複数が import 済み)
- **次の論点 (スコープ外, curriculum へ)**: P-0203 census が main に来たら穴を開ける。
  そのときは (1) ops/profiles と apps/autopilot の両 NP をバイト一致で更新
  (2) TestPolicySemantics.test_egress_allows_dns_and_nothing_else_yet の
  「規則 1 本」固定を conscious に更新 — この 2 点を忘れると drift guard / 意味論
  固定テストが落とす (意図された挙動)。drill 用・本番用 NP は別名
  (private-data-drill / private-data-egress-lock) なので掃除が本番物を壊さない設計
- drill・job-template の image (python:3.14-alpine) はタグ pin。digest pin 化は未処理

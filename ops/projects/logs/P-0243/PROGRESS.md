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

## セッション 3 (2026-08-23 深夜) — V2 の沈没原因を特定して二段構えで潰した。残る赤は環境欠陥で、merge+sync 後の新 Pod で初めて green になる

### やったこと (コミット順: ドリル堅牢化 → runner Pod 修正)

- **診断**: 受入 verify #2 の PermissionError は成果物の欠陥ではなく**実行環境の欠陥**
  と突き止めた。runner Pod のイメージに焼き込まれた `/tmp/opencode` が
  root:root 755 で、worker uid (10001) は sudo も setuid も mount も持たず
  一切書けない (`/work` は fsGroup 配下の emptyDir なのに /tmp だけボリューム外)。
  wrapper の受入走行も同一文脈なので、**全セッションの全再走が同じ場所で沈む**。
  前セッション (セッション 2) の「verify 再現済み」は別 Pod/uid 文脈だったと推定
- `48700a8e3`: exfil_drill.py を堅牢化。`check_report_destination()` が
  クラスタに触る前に書き出し先を実プローブし、書けなければ rc=2 で fail fast
  (副作用ゼロ, 実測 0.19 秒。従来はドリル完走後に PermissionError で無駄死に)。
  `write_report()` は同一ディレクトリ mkstemp → `os.replace` の原子的着地で、
  「前回残骸が他 uid 所有」の罠にも耐える。ドリル成立後に報告を書き損じたら
  rc=0 にしない (証拠の残らない成功は成功ではない)。unittest 3 本追加 (計 12 OK)
- `e7a45e365`: 根本修正。ops/heart/spawn.py の build_job が runner Pod の
  `/tmp/opencode` に fsGroup 10001 配下の emptyDir (64Mi) を mount。これで
  「/tmp/opencode は worker が使える作業場」という契約が初めて成立する。
  job-template.yaml (参照断片) にも同型を反映
- **ドリルを本 Pod (uid 10001, autopilot-writer) から実走し直し**:
  all_passed=true を 7.5 秒で再現 (labeled=拒否 / control=200 / 掃除 404 確認)。
  → 沈んでいたのは書き出し先権限**だけ**と実証。demo.json (台帳証拠) は触らず

### 現在地 (自分で実測した受入 3 項目)

- V1 (networkpolicy.yaml 存在): green
- V3 (readiness.json 基準 1 pass=true): green
- **V2: red — ただし失敗の質が変わった**。0.19 秒・クラスタ副作用ゼロで
  「report 先ディレクトリに書けない + 運用者への依頼文」を出して落ちる

### 次セッション / レビューへの引き継ぎ (重要)

- **この PR が merge され ArgoCD sync されるまで、V2 はどんなに再走しても
  red のままで正しい**。green 化は「merge → sync → 次回以降の runner Pod」で
  自動的に起こる (--report 先が fsGroup 配下になり原子的着地が通る)。
  sync 前の Pod 内でのリトライは時間の無駄 — 何も変わらない
- 本 PR は apps/ + ops/heart/ 含みで auto-merge 対象外 (why 冒頭で申告済み)。
  人間レビューの際は spawn.py への追記が volumeMount/volume 各 1 エントリの
  小差分であることだけ見ればよい。却下する場合の代替は「イメージ側で
  chmod 1777 /tmp/opencode」— どちらか一方で足りるが、emptyDir 方式は
  リポジトリ内で完結して検証可能
- セッション 2 の引き継ぎ事項はすべてそのまま有効: P-0203 census 到着時は
  両 NP バイト一致更新 + test_egress_allows_dns_and_nothing_else_yet の
  conscious 更新をセットで。digest pin 化も未処理

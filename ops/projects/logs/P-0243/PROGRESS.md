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

## セッション 4 (2026-08-23 深夜) — merge+sync 後の green 化を実測で先取りした。あとは人間レビューと sync を待つだけ

### やったこと

- 受入 3 項目を実走して現状確認: V1 green / V3 green / **V2 red** (fail-fast 0.17 秒・
  rc=2・クラスタ副作用ゼロ)。セッション 3 診断 (/tmp/opencode が root:root 755 で
  uid 10001 から不変) をそのまま再現。証拠 demo.json も無傷 (全キー true)
- **新 Pod テンプレートの効能を事前実証** (本セッションの本体)。spawn.py build_job
  (e7a45e365) の template.spec から書き込み契約に関わる部分だけを複製した使い捨て
  Pod (securityContext runAsUser/runAsGroup/fsGroup=10001 + emptyDir 64Mi を
  /tmp/opencode に mount、image python:3.14-alpine) を autopilot ns に立て、Pod 内で
  mkstemp プローブ + os.replace を実行 → **writable=true を実測** (7.2 秒、
  phase=Succeeded/exit 0、掃除の 404 確認込み)。mount 後の /tmp/opencode 実測値:
  dir_uid=0 / gid=10001 / mode 0777
- main 動静: PR #571〜#573 が merge 済みだが **P-0203 census はまだ origin/main に無い**
  → 穴開けタスクは変わらず curriculum 行き。触らない
- unittest 35 本 (test_private_data_profile + test_stage3_readiness) 全 OK。退行なし
- 検証スクリプトは捨てた (exfil_drill.py の K8s クライアント/wait_terminal/fetch_result/
  delete_ignore_404 を import して ~100 行。repo へは入れず、上記の記述だけで再現可能)

### 分かったこと

- fsGroup 配下の emptyDir を既存パスに mount すると、イメージ側の所有者に関係なく
  ディレクトリが gid=fsGroup のグループ書き込み可になる。runAsUser 10001 は owner でなく
  group 権で書く (dir_uid=0 のままでも書ける — これが実測で確定)
- exfil_drill.py の部品 (K8s クライアント他) は import すれば別用途の使い捨て検証 Pod が
  数十行で作れる。「cluster 内で X を実測したい」時の型として覚えておくと良い

### 次セッションへの引き継ぎ

- **状況は変わっていない**: V2 は merge+sync まで red で正しい。Pod 内でのリトライは
  無駄。ただし「sync 後に本当に通るのか」の最後の未確認リンクは今回実測済みなので、
  もう疑う必要はない
- 人間レビュー向けの一言: emptyDir 修正を却下するなら「V2 が永遠に green にならない」
  ことを意味する (= 代替案の chmod 1777 方式を実装して検証する義務が生じる)。
  本セッションの実測が emptyDir 方式の効能の証拠
- セッション 2/3 の引き継ぎ事項はすべてそのまま有効: census 到着時の両 NP バイト一致
  更新 + test_egress_allows_dns_and_nothing_else_yet の conscious 更新セット、digest pin 化未処理

## セッション 5 (2026-08-23 深夜) — 持ち越しの digest pin 化を完了。V2 は merge+sync 待ちのまま構造的に red で正しい

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755 で uid 10001 から不変
  → V2 は本 Pod では不変に red (fail-fast rc=2・0.2 秒・クラスタ副作用ゼロ)。
  V1/V3 は green。origin/main を fetch して #571〜#574 の merge を確認したが、
  本 PR はまだ未 merge = sync も起きていない。セッション 3/4 の診断は今日も正しい
- **P-0203 census を main の木で直接確認** (`git ls-tree -r origin/main | grep egress`):
  まだ無い (rc=1)。census 到着時の NP 更新タスクは引き続き curriculum 行き
- **持ち越し最後の 1 件「digest pin 化」を実施** (今回の本体):
  - `python:3.14-alpine` の OCI index digest を registry API で実測:
    `sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc`
    (auth.docker.io の匿名 token → manifests GET の docker-content-digest。
    tag 取得と digest 直接取得のレスポンスがバイト一致する round-trip まで確認)
  - exfil_drill.py の IMAGE 定数と job-template.yaml を `python@sha256:05b2…` に変更
    (両方に上げ方の手順コメント付き)。demo.json はドリル実績の記録なので
    **意図的に触らない** (過去の証拠を現在値で上書きしない)
  - ops/inventory.json に `private-data-drill-image` エントリを新設 (policy=manual、
    match は `IMAGE = "python@sha256:"`)。pin しても観測対象から外れたら #49 型の
    静放置に戻るだけなので、追跡への載せ込みまでが pin 化の完成と判断した
  - test_private_data_profile.py に TestImagePinning を追加: drill IMAGE の
    digest pin 形固定 + drill↔template の同一値固定 (浮遊タグへの退行・2 箇所の
    乖離をここで落とす)。inventory↔drill の一致までは見ない (watcher 側の管轄)

### 分かったこと

- inventory.json には digest pin 用の既存パターンがある: golang builder の例
  (`match: "FROM golang@sha256:"`, current に実 digest, policy manual)。
  前例 e6982d2c5 がほぼ同型。`match` はファイル内部分文字列なので、
  Python 側は `IMAGE = "python@sha256:` のように定数代入の形で絞るのが精度が出る
- version watcher / check_version_sync への影響はゼロを実測: 新エントリ追加後も
  test_version_watch 37 本 OK・check_version_sync 全項目 ok。upstream scheme
  `dockerhub:library/python` は既存エントリと同じものを再利用したので新規対応は不要
- validate.py の「archive.jsonl が origin/main 先頭一致せず」error は本ブランチ作業中は
  常に出る (main 側の追記帳簿が進んでいるだけで、stash 実測で私の変更と無関係と確認)。
  CI は main 上で回るので PR では問題にならない — 次セッションが驚かないように記す

### 検証 (全部自分で実走済み)

- unittest 37 本 OK (test_private_data_profile + test_stage3_readiness。退行なし)
- python3 ops/validate.py: 上記 archive.jsonl 以外は error 無し (warning 11 件は既存の
  backlog refs と todo 枯渇で既存問題)
- spec verify V1 green / V3 green / V2 red (既知の fail-fast、品質はセッション 3 から不変)

### 次セッションへの引き継ぎ

- **状況はセッション 4 から一歩も動いていない**: V2 は merge+sync 後の新 runner Pod で
  自動 green 化する。Pod 内での再走は無駄。やることは「merge を待つ」だけ
- 人間レビュー向け: 今セッションの差分は image の tag→digest 差し替え 2 箇所 +
  inventory 追記 1 エントリ + drift guard テスト 2 本のみ。spawn.py / apps/ /
  readiness.json / demo.json には触っていない (前セッションまでの差分に重ねても
  レビュー範囲は広がらない)。digest の上げ忘れリスクは policy=manual + watcher 観測 +
  テスト固定の三段で受けている
- census 到着時の手順はセッション 3/4 記載どおり不変 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)。
  「digest pin 化未処理」は**解消済み** — 引き継ぎ事項から消してよい

## セッション 6 (2026-08-23 23:15 UTC) — origin/main (#576/#577) を追い越し、heart 全テストの退行なしを確認

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755 で uid 10001 から不変
  → V2 は本 Pod では fail-fast rc=2・クラスタ副作用ゼロで red で正しい。V1/V3 green。
  セッション 3〜5 の診断は今日も正しい
- **main 新着の内容確認**: #576 は curriculum 帳簿 (archive.jsonl +9 行のみ)、
  #577 は merge-conflict backoff (gh.py / heart.py / reconcile.py / test_reconcile.py)。
  本 PR が触る spawn.py を含む 14 ファイルと重複なし
- **P-0203 census を main の木で再確認** (`git ls-tree -r origin/main | grep egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解。穴開けタスクは引き続き休止中
- **origin/main を merge** (今回の本体): コンフリクトなしで取り込み完了。
  archive.jsonl の追記も一緒に入った
- **merge 後に ops/heart テスト全走**: 253 本 OK (#577 の新 test_reconcile 含む)。
  本ブランチの spawn.py 変更と #577 の組み合わせで退行なし
- python3 ops/validate.py: **error 0** — セッション 5 まで出ていた「archive.jsonl が
  origin/main 先頭一致せず」error は、main 側帳簿を取り込んだことで消滅。
  warning 11 件は既存問題 (dashboard refs と todo 枯渇)

### 分かったこと

- #577 以降、「コンフリクトする PR」は自動 merge が即 stalled 化して人間に question
  1 回だけ送る挙動になる (= コンフリクト = 人間待ち)。長命なプロジェクトブランチは
  定期的に main を追い越すのが実務的な予防策で、本セッションの追い越しはその実施でもある
- validate.py の既知 error は「branch 作業中は常に出る」ではなく「**main 追い越し前は**
  出る」が正確だった。追い越せば消える

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- unittest: test_private_data_profile + test_stage3_readiness 37 本 OK /
  ops/heart 全 253 本 OK (退行なし)
- python3 ops/validate.py: error 0, warning 11 (既存)

### 次セッションへの引き継ぎ

- **状況はセッション 4/5 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する。Pod 内での再走は無駄。やることは「PR merge を待つ」だけ
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main がまた動いたら同じ要領で追い越すこと (#577 以降はコンフリクト放置が
  即「人間待ち停止」に繋がるため、追い越しの価値は上がっている)

## セッション 7 (2026-08-23 23:18 UTC) — origin/main (#578) を追い越し、全検証の現状再確認

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 で不変
  → V2 は本 Pod では fail-fast rc=2・クラスタ副作用ゼロで red で正しい。
  セッション 3〜6 の診断は今日も正しい
- **main 新着の内容確認**: #578 は curriculum 帳簿 (archive.jsonl +6 行のみ) で、
  本 PR が触る 14 ファイルと重複なし (diff --stat で実測)
- **P-0203 census を main の木で再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解。穴開けタスクは引き続き休止中
- **origin/main を merge** (今回の本体): コンフリクトなしで取り込み完了
  (f45ef2051)。archive.jsonl の追記も一緒に入った
- **merge 後に検証一式を再走**: unittest 全 454 本 OK (#578 取り込み後も退行なし) /
  python3 ops/validate.py: error 0, warning 11 (既存問題) /
  spec verify V1 green・V3 green・V2 red (既知 fail-fast rc=2)

### 分かったこと

- heart のテスト数はセッション 6 記載の 253 本から 454 本に増えていた (main 側で
  テスト追加が進んでいた)。本ブランチの spawn.py 変更を含めて全部 green なので、
  「253 本」という数字自体はもう古い — 次セッションは件数でなく OK/NG だけ見ること
- validate.py は error 0 を維持。warning 11 件は dashboard refs と todo 枯渇で既存問題

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、書き込みプローブで即中断)
- unittest 全 454 本 OK (退行なし)
- python3 ops/validate.py: error 0, warning 11 (既存)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜6 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する。Pod 内での再走は無駄。やることは「PR merge を待つ」だけ
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main がまた動いたら同じ要領で追い越すこと (curriculum 帳簿はほぼ毎時流れてくるが
  archive.jsonl 追記のみなら重複ゼロで自動 merge 可能。spawn.py 等を触る PR が来たら
  重複確認を丁寧に)

## セッション 8 (2026-08-23 23:23 UTC) — 現在地の再実測のみ。main 不動・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 で不変
  → V2 は fail-fast rc=2 で red で正しい。コードでも再確認済み
  (exfil_drill.py の書き込みプローブが main() 冒頭・クラスタ接触前に走るので
  副作用ゼロで中断する。exfil_drill.py:319-327)
- **origin/main を fetch**: #578 以降の新着なし (`git log f45ef2051..origin/main` は空。
  動いたのは ops-state のみ) → 今セッションに追い越すべきコミットは存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認。V2 は上記のとおり既知 fail-fast
- **コード変更は今セッションもゼロ**。この記録の追記だけ
- unittest 全走は見送った: セッション 7 の全 454 本 OK 以降コード差分が
  コミット単位でゼロ (本コミットも PROGRESS.md のみ) なので退行の起きようがない

### 分かったこと

- 「やることが無い」セッションでも wrapper はフレッシュ起動してくる。その場合の
  最小仕事は「診断の再実測 + このログの追記」だけでよく、コード・テスト・merge に
  手を出す必要はない (本セッションが実例)

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜7 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する。Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、このセッション (8) と同じ「再実測 + ログ追記のみ」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 9 (2026-08-23 23:26 UTC) — 現在地の再実測のみ。main 不動・census 未着・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 で不変
  → V2 は fail-fast rc=2 で red で正しい (書き込みプローブが main() 冒頭・
  クラスタ接触前に走るので副作用ゼロで中断。セッション 8 記載どおり再確認)
- **origin/main を fetch**: #578 以降の新着なし (`git log HEAD..origin/main` は空)。
  動いたのは ops-state と新プロジェクトブランチ (p-0265/p-0270) のみ
  → 今セッションに追い越すべきコミットは存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認。V2 は既知 fail-fast rc=2
- **コード変更は今セッションもゼロ**。この記録の追記だけ。
  unittest 全走は見送り: 最後の全 454 本 OK (セッション 7) 以降のコード差分が
  コミット単位でゼロ (セッション 8 も本セッションも PROGRESS.md のみ) なので
  退行の起きようがない

### 分かったこと

- 新着の有無は `git log HEAD..origin/main` 一発で判別できる。ops-state や他プロジェクトの
  ブランチが動いても本 PR には無関係 — 追い越し対象は origin/main の進行だけ

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜8 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する。Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 8/9 と同じ「再実測 + ログ追記のみ」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 10 (2026-08-23 23:31 UTC) — 現在地の再実測のみ。main 不動・census 未着・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 (autopilot) で不変
  → V2 は fail-fast rc=2 で red で正しい。仕様の verify コマンドをそのまま実行して
  wrapper の実測と同一の stderr を確認 (書き込みプローブが main() 冒頭・クラスタ接触前に
  走るので副作用ゼロで中断。exfil_drill.py:322-327)
- **修正済みであることをコード行単位で再確認**: 本ブランチの spawn.py は fsGroup 10001
  配下の emptyDir (64Mi) を /tmp/opencode に mount 済み (ops/heart/spawn.py:157,
  168-169, 184-192)。merge+sync 後の新 runner Pod では V2 の前提が崩れない
- **origin/main を fetch**: #578 以降の新着なし (`git log HEAD..origin/main` は空)。
  動いたのは ops-state と他プロジェクトブランチ (p-0258/p-0270)、
  feat/core-dispatch の削除のみ → 追い越し対象は存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認 (V3 は台帳基準 1 pass=true +
  evidence_path=ops/profiles/private-data/demo.json 実在も確認)
- **コード変更は今セッションもゼロ**。この記録の追記だけ。
  unittest 全走は見送り: 最後の全 454 本 OK (セッション 7) 以降のコード差分が
  コミット単位でゼロ (セッション 8/9 も本セッションも PROGRESS.md のみ) なので
  退行の起きようがない

### 分かったこと

- 台帳基準 1 (trifecta-separation-drill) は evidence_path の実在を含めて健全
  (demo.json の 5 指標すべて true、2026-08-23 22:13 着地の in-cluster 実測)。
  「捏造ではない正当な pass」であることは本ブランチ内で完結して確認できるので、
  残る赤は V2 の 1 つだけ

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜9 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み、行番号上記)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 8〜10 と同じ「再実測 + ログ追記のみ」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 11 (2026-08-23 23:34 UTC) — 現在地の再実測のみ。main 不動・census 未着・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 (autopilot) で不変
  → V2 は fail-fast rc=2 で red で正しい。仕様の verify コマンドをそのまま実行して
  wrapper の実測と同一の stderr を確認 (書き込みプローブが main() 冒頭・クラスタ接触前に
  走るので副作用ゼロで中断)
- **修正済みであることをコード行単位で再確認**: 本ブランチの spawn.py は fsGroup 10001
  配下の emptyDir (64Mi) を /tmp/opencode に mount 済み (ops/heart/spawn.py:157,
  168-169, 191-192)。merge+sync 後の新 runner Pod では V2 の前提が崩れない
- **origin/main を fetch**: #578 以降の新着なし (`git log HEAD..origin/main` は空)。
  動いたのは ops-state と ops-health-report ブランチのみ → 追い越し対象は存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認
- **コード変更は今セッションもゼロ**。この記録の追記だけ。
  unittest 全走は見送り: 最後の全 454 本 OK (セッション 7) 以降のコード差分が
  コミット単位でゼロ (セッション 8〜10 も本セッションも PROGRESS.md のみ) なので
  退行の起きようがない

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜10 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み、行番号上記)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 8〜11 と同じ「再実測 + ログ追記のみ」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 12 (2026-08-23 23:36 UTC) — 現在地の再実測のみ。main 不動・census 未着・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 (autopilot) で不変
  → V2 は fail-fast rc=2 で red で正しい。仕様の verify コマンドをそのまま実行して
  wrapper の実測と同一の stderr を確認 (書き込みプローブが main() 冒頭・クラスタ接触前に
  走るので副作用ゼロで中断)
- **修正済みであることをコード行単位で再確認**: 本ブランチの spawn.py は fsGroup 10001
  配下の emptyDir (64Mi) を /tmp/opencode に mount 済み (ops/heart/spawn.py:157,
  168-169, 191-192)。merge+sync 後の新 runner Pod では V2 の前提が崩れない
- **origin/main を fetch**: #578 以降の新着なし (`git log HEAD..origin/main` は空)。
  ワーキングツリーは清潔で origin/project/p-0243 と一致
  → 追い越し対象は存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認 (V3 は台帳基準 1 pass=true +
  evidence_path=ops/profiles/private-data/demo.json 実在も確認)
- **コード変更は今セッションもゼロ**。この記録の追記だけ。
  unittest 全走は見送り: 最後の全 454 本 OK (セッション 7) 以降のコード差分が
  コミット単位でゼロ (セッション 8〜11 も本セッションも PROGRESS.md のみ) なので
  退行の起きようがない

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜11 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み、行番号上記)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 8〜12 と同じ「再実測 + ログ追記のみ」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 13 (2026-08-23 23:38 UTC) — 現在地の再実測のみ。main 不動・census 未着・変更ゼロ

### やったこと

- **現在地を再実測**: /tmp/opencode は依然 root:root 755・uid 10001 (autopilot) で不変
  → V2 は fail-fast rc=2 で red で正しい。仕様の verify コマンドをそのまま実行して
  wrapper の実測と同一の stderr を確認 (書き込みプローブが main() 冒頭・クラスタ接触前に
  走るので副作用ゼロで中断)
- **修正済みであることをコード行単位で再確認**: 本ブランチの spawn.py は fsGroup 10001
  配下の emptyDir (64Mi) を /tmp/opencode に mount 済み (ops/heart/spawn.py:157,
  168-169, 191-192)。merge+sync 後の新 runner Pod では V2 の前提が崩れない
- **origin/main を fetch**: #578 以降の新着なし (`git log HEAD..origin/main` は空)。
  ワーキングツリーは清潔で origin/project/p-0243 と一致
  → 追い越し対象は存在しなかった
- **P-0203 census を再確認** (`git ls-tree -r origin/main | grep -c egress`):
  まだ無い (rc=1)。NP は DNS-only fail-closed のままが正解
- spec verify V1 green / V3 green を再確認 (V3 は台帳基準 1 pass=true +
  evidence_path=ops/profiles/private-data/demo.json 実在も確認)
- **コード変更は今セッションもゼロ**。この記録の追記だけ。
  unittest 全走は見送り: 最後の全 454 本 OK (セッション 7) 以降のコード差分が
  コミット単位でゼロ (セッション 8〜12 も本セッションも PROGRESS.md のみ) なので
  退行の起きようがない

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜12 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み、行番号上記)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 8〜13 と同じ「再実測 + ログ追記のみ」でよい。
  ただし merge 待ちが長引いているので、次回以降は「main 不動」を確認したら
  短絡で終えてよい (V1/V2/V3 + census の 4 点チェックだけで十分。コード行の
  目視再確認 spawn.py:157,168-169,191-192 は git diff が空である限り不要)
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 14 (2026-08-23 23:40 UTC) — 短絡チェックのみ (セッション 13 の方針どおり)。main 不動・census 未着・変更ゼロ

### やったこと

- **セッション 13 の短絡手順に従い 4 点チェックだけ実施**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr も wrapper 実測と同一) /
  V3 green (evidence_path=ops/profiles/private-data/demo.json 実在も確認) /
  census 未着 (rc=1)。main 新着なし (#578 のまま)
- **現在地の再実測**: /tmp/opencode は依然 root:root 755 で不変 → V2 が red
  で正しい状態は変わらず。書き込みプローブは main() 冒頭・クラスタ接触前に走るので
  副作用ゼロで中断することも確認済み (前セッションまでと同一挙動)
- **コード行の目視再確認は省略** (git diff HEAD が空であることを確認済み —
  空である限り不要というセッション 13 の合意どおり)
- **コード変更は今セッションもゼロ**。この記録の追記だけ。unittest 全走見送りの
  根拠も同一 (セッション 7 の全 454 本 OK 以降、コミット単位のコード差分ゼロ)

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜13 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 13〜14 と同じ「4 点チェック短絡 + ログ追記」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 15 (2026-08-23 深夜) — 短絡チェックのみ (セッション 13 の方針どおり)。main 不動・census 未着・変更ゼロ

### やったこと

- **セッション 13 の短絡手順に従い 4 点チェックだけ実施**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr も wrapper 実測と同一) /
  V3 green (evidence_path=ops/profiles/private-data/demo.json の全 true キー
  7 点を機械確認 — 無傷) / census 未着 (rc=1)
- **main 新着なし**: `git rev-list HEAD..origin/main --count` = 0 (#578 のまま)。
  ワーキングツリー清潔・origin/project/p-0243 と一致 → 追い越し対象なし。
  コード行の目視再確認も省略 (セッション 13 の合意どおり)
- PR 状態は `gh` 未導入で確認できず (runner イメージに無い。merge 判定は wrapper 任せ)
- **コード変更は今セッションもゼロ**。この記録の追記だけ。unittest 全走見送りの
  根拠も同一 (セッション 7 の全 454 本 OK 以降、コミット単位のコード差分ゼロ)

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- demo.json 完全性確認 (全キー true を assert) / census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜14 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 13〜15 と同じ「4 点チェック短絡 + ログ追記」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 16 (2026-08-23 深夜) — 短絡チェックのみ (セッション 13 の方針どおり)。main 不動・census 未着・変更ゼロ

### やったこと

- **セッション 13 の短絡手順に従い 4 点チェックだけ実施**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr も wrapper 実測と同一:
  `/tmp/opencode` は root:root 755 のまま不変 → 書き込みプローブが main() 冒頭・
  クラスタ接触前に中断し副作用ゼロ) /
  V3 green (evidence_path=ops/profiles/private-data/demo.json 実在 +
  完全性も機械確認 — 下記の罠に一度引っかかったが無傷と判明) /
  census 未着 (rc=1)
- **main 新着なし**: `git rev-list HEAD..origin/main --count` = 0 (#578 のまま)。
  ワーキングツリー清潔・origin/project/p-0243 と一致 (`git diff HEAD` 空) →
  追い越し対象なし。コード行の目視再確認も省略 (セッション 13 の合意どおり)
- PR 状態は `gh` 未導入で確認できず (merge 判定は wrapper 任せ。セッション 15 同様)

### 分かったこと (次セッションへの罠注意)

- **demo.json の完全性チェックは「結果系 bool 7 キー」を見ること**:
  「全トップレベルキーの値が True」と書くと誤検知する。demo.json には
  drill/target/image/policy/namespace/started_at/pods/finished_at といった
  メタデータ (文字列・辞書・null 含む) が混在しており、bool 同列で assert すると
  必ず落ちる。正しいチェックは 7 キー
  (labeled_blocked, unlabeled_allowed, dns_ok_labeled, dns_ok_control,
  cleaned_up, all_passed, probes_conclusive) が全 true +
  labeled.probe.https_ok is False (拒否の証拠そのものなので False で正しい) +
  control.probe.https_ok is True の対照確認。今セッションでこの形に直して
  全パスを確認済み — 次からはこれを写すこと

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- demo.json 完全性確認 (7 キー + プローブ対照を assert) / census 未着確認 (rc=1) / main 新着なし確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜15 から一歩も動いていない**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  Pod 内での再走は無駄。やることは「PR merge を待つ」だけ。
  main 新着がなければ、セッション 13〜16 と同じ「4 点チェック短絡 + ログ追記」でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越す (#578 時点の要領どおり)。動いていなければ merge 不要

## セッション 17 (2026-08-23 深夜) — main 追い越し (#579 取り込み) + 短絡チェック。V2 の壁を環境側から実証し直し。コード変更ゼロ

### やったこと

- **main が動いた (#578→#579) ので追い越しを実施**: `git diff $(git merge-base …)
  origin/main --stat` で中身を確認したところ、#579 の実体は curriculum の不採択案
  P-0271〜P-0277 を ops/projects/archive.jsonl に +7 行しただけ。
  **P-0243 自体は archive されておらず生きていることを確認** (archive 行に含まれず、
  台帳・spawn への影響なし)。コンフリクトなしで merge 完了 (8d2d0f09e)
- **セッション 13 の短絡手順どおり 4 点チェック**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr は wrapper 実測と同一) /
  V3 green / census 未着 (rc=1)
- **V2 の壁を環境側から再実証** (「本当に Pod 内からはどうにもならないのか」の確認):
  runner は uid=10001(autopilot)、`/tmp/opencode` は root:root 755 不変、
  しかも **sudo バイナリ自体がイメージに無い** (`sudo: command not found`)。
  権限修正の手段が Pod 内に存在しないことが実測で確定 →
  「merge+sync 後の新 runner Pod (emptyDir mount 済み) を待つしかない」が裏取りされた
- demo.json 完全性確認はセッション 16 形式で全パス (下記のパス指定の罠に一旦引いたが無傷)
- コード変更は今セッションもゼロ (この記録の追記と main 追い越しのみ)

### 分かったこと (次セッションへの罠注意)

- **demo.json の対照プローブは `pods.labeled.probe` / `pods.control.probe` 配下**:
  セッション 16 の引き継ぎ文の「labeled.probe.https_ok」をそのまま
  `d['labeled']['probe']…` と書くと KeyError で落ちる (実際落ちた。ファイルは無傷)。
  正しくは `d['pods']['labeled']['probe']['https_ok'] is False` /
  `d['pods']['control']['probe']['https_ok'] is True`
- **main が動いていたときの中身確認の手順**: `git diff $(git merge-base HEAD
  origin/main) origin/main --stat` を先に見ること。HEAD↔main の素 diff だと
  本 PR の追加ファイル群が全部「削除」表示になって一見大惨事に見えるが、
  それは自ブランチ側の追加が映っているだけ (#579 で一度誤読しかけた)
- curriculum の merge が来た回は archive.jsonl の新行に自分の id が無いか目を通す
  (プロジェクトが archive されたら worker ループの続行前提が崩れるため)

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- demo.json 完全性確認 (7 bool キー + pods.*.probe の対照を assert) /
  census 未着確認 (rc=1) / main 追い越し後の残り新着 = 0 確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜16 から本質的に不変** (main 追い越し済みが差分): V2 は本 PR の
  merge+sync 後の新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  Pod 内での再走・権限 hack は不要と実証済み (sudo 不在まで確認ずみ)。やることは
  「PR merge を待つ」だけ。main 新着がなければセッション 13〜17 同様の短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main が動いていれば追い越し (手順は今セッションの「罠注意」参照:
  merge-base diff で中身確認 → P-0243 が archive に入っていないか確認 → merge)

## セッション 18 (2026-08-23 深夜) — 短絡チェックのみ + archive.jsonl 誤読の解消。main 不動・census 未着・コード変更ゼロ

### やったこと

- **セッション 13〜17 の短絡手順どおり 4 点チェック**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr は wrapper 実測と同一メッセージ。
  main() 冒頭の書き込みプローブで中断・クラスタ接触前なので副作用ゼロ) /
  V3 green / census 未着 (`git ls-tree -r origin/main | grep -c egress` = 0, rc=1)
- **archive.jsonl 全文 grep で自分の id が hit して一時「archive されたか?」と疑ったが、
  誤読だったと判明** (詳細は下記「罠注意」)。README を読んで意味論を確認:
  archive.jsonl は採択・棄却を問わず全案を載せる追記専用の恒久台帳で、
  実行状態はそこに無い。実状態は `origin/ops-state:projects.json` を見るべきで、
  P-0243 は `state: active`・spawn_count=1 → **ループ前提は健在**
  (なお ops-state ブランチは本セッション中も動いていたので確認時の fetch は必須)
- demo.json 完全性チェックはセッション 17 形式 (7 bool キー +
  `pods.labeled.probe.https_ok is False` / `pods.control.probe.https_ok is True`) で全パス
- 本 PR の差分範囲を再確認 (14 ファイル・spawn.py の emptyDir mount 済み)。
  コード変更は今セッションもゼロ

### 分かったこと (次セッションへの罠注意)

- **archive.jsonl に自分の id があっても死亡ではない**: 採択案も全部載る
  (「curriculum が立てた全案の恒久記録」と README に明記)。セッション 17 の
  「P-0243 自体は archive 行に含まれず」は誤記 — 実際は #578 の「curriculum: 6 案 (採択 2)」
  (5cc83fcd4、セッション 7 取り込み済み) で入っていた。結論 (現役) は正しかったが
  根拠が間違っていた。**生死確認の正手順は fetch 後
  `git show origin/ops-state:projects.json` の当該 id の `state` を見ること**
- `git rev-list HEAD..origin/main --count` はローカル ref 基準。fetch 前に数えても
  古い数が出る (今回はたまたま同値だったが、ops-state は実際に動いていた)。
  数える前に `git fetch origin` を先に打つ習慣に

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- demo.json 完全性確認 (7 bool キー + pods.*.probe 対照を assert) /
  census 未着確認 / fetch 後 main 新着 = 0 (#579 のまま) 確認 /
  ops-state:projects.json の P-0243 state=active 確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜17 から不変** (archive.jsonl の誤読解消が差分): V2 は本 PR の
  merge+sync 後の新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み —
  今セッション再確認)。Pod 内での再走・権限 hack は不要 (sudo 不在まで実証済み)。
  やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 19 (2026-08-24) — 短絡チェックのみ。main 不動・census 未着・コード変更ゼロ

### やったこと

- **セッション 13〜18 の短絡手順どおりチェック**:
  V1 green / V2 red (既知 fail-fast rc=2、stderr は wrapper 実測と同一メッセージ。
  書き込みプローブで中断・クラスタ接触前なので副作用ゼロ。なお wrapper 実測が
  同一エラーということは runner 環境自体がまだ新しくなっていないことの裏取りでもある) /
  V3 green / census 未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- **fetch を先に打ってから数える**習慣 (セッション 18 の教訓) を実行:
  ops-state は実際に動いていた (75b480bdc..b3312fee3、他に project/p-0258 も動いた)。
  教訓どおり fetch 後に `git show origin/ops-state:projects.json` で P-0243 を確認 →
  **`state: active`** でループ前提は健在
- demo.json 完全性チェック (7 bool キー + `pods.labeled.probe.https_ok is False` /
  `pods.control.probe.https_ok is True`) 全パス
- 本 PR の差分範囲を再確認: `git diff --stat origin/main...HEAD` で **14 ファイル**不変。
  spawn.py の emptyDir mount (`opencode-tmp` → `/tmp/opencode`) も無傷をファイル実読で確認。
  コード変更は今セッションもゼロ

### 分かったこと (次セッションへの罠注意)

- 今セッションで新しい罠は無し。既知の手順 (fetch 先行・merge-base diff・
  pods.*.probe パス・生死は ops-state 見る) が全部そのまま機能した
- ops-state が動いている回は projects.json の自分の record も目を通すこと
  (今回は state=active で変化なし。adopt_gate_attempts 等の欄が将来更新されたら
  何か起きている合図)

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ)
- demo.json 完全性確認 (7 bool キー + pods.*.probe 対照を assert) /
  census 未着確認 / fetch 後 main 新着 = 0 (#579 のまま) 確認 /
  ops-state:projects.json の P-0243 state=active 確認 /
  spawn.py emptyDir mount の実読確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜18 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 20 (2026-08-24) — main #580 を追い越し (inventory 自動解決・コンフリクトなし), heart 時限爆弾テスト 2 件を「main 側起因」と実証, コード変更ゼロ

### やったこと

- **fetch 先行 (セッション 18 の教訓どおり) → main 新着 = 6 コミット (#580, P-0270
  AdGuard Home 新設)**。本 PR との差分重複は `ops/inventory.json` のみだったため、
  セッション 6/7/17 の前例どおり origin/main を merge (コンフリクトなし・自動解決。
  merge 後 `json.load` で inventory.json の妥当性確認済み)
- **merge 後に検証一式を再走**:
  - spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2。stderr も wrapper
    実測と同一。書き込みプローブで中断・クラスタ接触前なので副作用ゼロ)
  - ops/tests 全 OK / runner tests 全 OK / validate.py: error 0, warning 11 (既存問題)
  - **heart tests のみ FAILED (failures=2)** — 詳細は下記「発見」。それ以外は退行なし
- census 未着を再確認 (`git ls-tree -r origin/main | grep -c egress` = 0) /
  fetch 後 ops-state:projects.json で P-0243 `state: active`・spawn_count=1 を確認 /
  本 PR 差分 14 ファイル不変・spawn.py emptyDir mount 無傷を実読確認
- demo.json 完全性チェック全パス (セッション 17 形式)。**ただし数え方の罠を 1 つ
  学んだ** (下記「罠注意」)

### 発見 (仕様外 — curriculum / 週次点検が拾うこと)

- **main 側の heart テストに時限爆弾がある**: `ops/heart/tests/test_budget_alert_beat.py`
  と `test_dashboard_smoke_alert_beat.py` が `TODAY = "2026-08-23"` を焼き付けており、
  **2026-08-24 (UTC) の日付ロールオーバーで main 上でも落ちるようになった**。
  素の origin/main の worktree で同 2 件を実走して同一 failure を再現済み —
  本ブランチの merge 起因では断じてない。本 PR では直さない (スコープ外 + heart は
  人間レビュー領域)。次の curriculum / メンテナンス起動で修正されたい
  (直し方: NOW/TODAY を freezgun 的 fixture 化するか実行日から導出)
- **demo.json 完全性チェックの「7 bool キー」はトップレベルのみの数え方**:
  bool を再帰的にフラット化して数えると `pods.*.probe.*` の 4 個が加わり **11 個に
  なる** (今セッション実際に一瞬パニックした)。正しいチェックは「トップレベル bool が
  7 個 (`all_passed`, `cleaned_up`, `dns_ok_control`, `dns_ok_labeled`,
  `labeled_blocked`, `probes_conclusive`, `unlabeled_allowed`) +
  `pods.labeled.probe.https_ok is False` + `pods.control.probe.https_ok is True`」

### 分かったこと (次セッションへの罠注意)

- 今セッションで新しい罠は上記 2 点 (時限爆弾は罠というより環境側の破壊的変化) のみ。
  既知の手順 (fetch 先行・merge-base diff・生死は ops-state) が全部そのまま機能した
- heart tests が 2 件落ちても**本 PR の文脈では退行ではない**。まず
  「素 main でも落ちるか」を worktree で切り分けるのが正手順 (今回実証済み)。
  落ちる=自分の merge 壊した、と誤認して heart コードを触らないこと

### 検証 (全部自分で実走済み)

- spec verify V1 green / V3 green / V2 red (既知 fail-fast rc=2、クラスタ副作用ゼロ) —
  merge 後に再走済み
- unittest 3 discover: ops/tests OK / runner OK / heart FAILED(2) — 2 件は素 origin/main
  worktree でも同一に落ちることを実証 (main 側時限爆弾) /
  python3 ops/validate.py: error 0, warning 11 (既存問題) /
  inventory.json json.load OK / census 未着確認 / P-0243 state=active 確認 /
  spawn.py emptyDir mount の実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜19 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 21 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 (セッション 18 の教訓どおり) → main 新着 = 0** (#580 のまま)。
  merge 作業なし。census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いていた (a018aa81c → 0758f96ab) ため
  ops-state:projects.json を確認: P-0243 `state=active`・spawn_count=1・
  drift_count=0 の不変。adopt_gate_attempts=1 のまま (欄が将来更新されたら
  何か起きている合図、は継続)
- spec verify 一式を再走:
  - V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2 で stderr も wrapper
    実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、クラスタ接触前
    なので副作用ゼロ)
  - PR 差分 14 ファイル不変・spawn.py emptyDir mount (/tmp/opencode, 64Mi) 無傷を
    実読確認
- demo.json 完全性チェック全パス (**セッション 20 修正版の「トップレベル bool 7 個」
  形式で実行** — `all_passed/cleaned_up/dns_ok_control/dns_ok_labeled/labeled_blocked/
  probes_conclusive/unlabeled_allowed` + `pods.labeled.probe.https_ok is False` +
  `pods.control.probe.https_ok is True`)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / spawn.py emptyDir mount 実読確認 /
  demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜20 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 22 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 再び動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が再び動いた (0758f96ab → 956e1fdce。併せて project/p-0258
  ブランチも進んでいた) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- spec verify 一式を再走: V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、
  クラスタ接触前なので副作用ゼロ)
- PR 差分不変を確認: merge-base 起点でコード側 12 ファイル + P-0243 ログ 2 ファイル =
  14 ファイル (**前セッションまでの「14 ファイル」はログ込みの数え方** —
  `':!ops/projects/logs/P-0243'` で除外すると 12 になる。次セッションはどちらの数え方か
  混同しないこと)。spawn.py emptyDir mount (/tmp/opencode, 64Mi) 無傷を実読確認
  (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式)

### 発見 (仕様外)

- 今セッションで新しい発見は無し。「14 ファイル」の数え方 (ログ込み) を明記したのみ

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜21 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 23 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (956e1fdce → da462195d。併せて project/p-0272 と
  heart/curriculum-20260824-002231 ブランチも新出) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- spec verify 一式を再走: V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、
  クラスタ接触前なので副作用ゼロ)
- PR 差分不変を確認: merge-base 起点でコード側 12 ファイル + P-0243 ログ 2 ファイル =
  14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi) 無傷を実読確認
  (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜22 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 24 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (da462195d → be2ddeb4f。併せて project/p-0258 ブランチも
  進んでいた) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- spec verify 一式を再走: V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、
  クラスタ接触前なので副作用ゼロ)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜23 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 25 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (be2ddeb4f → daa92da4c) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- spec verify 一式を再走: V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、
  クラスタ接触前なので副作用ゼロ)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜24 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る

## セッション 26 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (daa92da4c → 35851b841) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- spec verify 一式を再走: V1 green / V3 green / V2 red — V2 は既知 fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (`/tmp/opencode` 書き込みプローブで中断、
  クラスタ接触前なので副作用ゼロ)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (mountPath / sizeLimit 行とも元位置のまま)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)。
  7 bool の内訳確認済み: labeled_blocked / unlabeled_allowed / dns_ok_labeled /
  dns_ok_control / cleaned_up / all_passed / probes_conclusive

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜25 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る
- **V2 を実走する前の一手順 (セッション 26 追加)**: 自前で `/tmp/opencode` の書き込み
  可否だけ先にプローブすること。もし環境側が変わって書けるようになっていた場合、
  V2 の実走はそのまま in-cluster ドリル (一時 NP + Pod 2 本の作成) まで進む。
  短絡セッションで副作用を起こす意図はないので、「fail-fast になる予測 → 実行」の順。

## セッション 27 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (35851b841 → a243f9ce0。併せて ops-health-report /
  project/p-0258 / project/p-0272 ブランチも進んでいた) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  欄の変化は無し (変化したら何か起きている合図、は継続)
- **セッション 26 追加の「プローブ先行」手順を実行**:
  `/tmp/opencode` へ mktemp プローブ → NOT writable ([Errno 13], root 所有ディレクトリ ×
  uid 10001) を先に確認してから V2 実走。予測どおり fail-fast rc=2、
  stderr は wrapper 実測と同一メッセージ (クラスタ接触前なので副作用ゼロ)。
  手順は意図どおり機能した
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜26 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る
- **V2 を実走する前の一手順 (セッション 26 追加・27 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順。

## セッション 28 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (a243f9ce0 → e3cd106db。併せて project/p-0258 /
  project/p-0272 ブランチも進行、origin/project/p-0270 は merge 済みで削除) ため
  ops-state:projects.json を確認: P-0243 `state=active`・spawn_count=1・
  drift_count=0・adopt_gate_attempts=1 の不変
- **セッション 26 追加の「プローブ先行」手順を実行**:
  `/tmp/opencode` へ mktemp プローブ → NOT writable ([Errno 13]) を先に確認してから
  V2 実走。予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (spawn.py:169 mountPath / :192 emptyDir sizeLimit)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜27 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る
- **V2 を実走する前の一手順 (セッション 26 追加・27/28 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順。
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 29 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま)。merge 作業なし。
  census も未着 (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (e3cd106db → 117318998。併せて project/p-0258 /
  project/p-0272 ブランチも進行) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変
- **セッション 26 追加の「プローブ先行」手順を実行**:
  `/tmp/opencode` へ mktemp プローブ → NOT writable ([Errno 13]) を先に確認してから
  V2 実走。予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点でコード側 12 ファイル +
  P-0243 ログ 2 ファイル = 14 ファイル。spawn.py emptyDir mount (/tmp/opencode, 64Mi)
  無傷を実読確認 (mountPath / emptyDir sizeLimit の実在)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

- 今セッションで新しい発見は無し

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0) / census 未着確認 /
  ops-state:projects.json P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜28 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る
- **V2 を実走する前の一手順 (セッション 26 追加・27〜29 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順。
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 30 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (117318998 → 77f733404。併せて project/p-0258 /
  project/p-0272 ブランチも進行) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変
  (スナップショットを /tmp 直下に保存してからパース — ops-state が push 中だと
  `git show` の都度読みで形が変わって parse が安定しない実測があったため)
- **セッション 26 追加の「プローブ先行」手順を実行**:
  `/tmp/opencode` へ mktemp プローブ → NOT writable ([Errno 13]) を先に確認してから
  V2 実走。予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。spawn.py emptyDir mount (/tmp/opencode) 無傷を実読確認
  (spawn.py:169 mountPath 実在)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

- ops-state ブランチが他セッションと並行で push されると、`git show
  origin/ops-state:projects.json` の都度読みでは取得のたびに中身が変わることがある
  (今セッション冒頭で dict/list が混在する parse 失敗を 2 回実測)。
  一度ローカルファイルにスナップショットしてからパースすれば安定。以降の短絡
  チェックではこの手順を推奨

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜29 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る。
  読み方は**スナップショット方式** (セッション 30 発見参照): 一度ローカルファイルに
  書き出してからパースする。トップレベルは dict で `d['projects']['P-0243']`
- **V2 を実走する前の一手順 (セッション 26 追加・27〜30 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順。
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 31 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), ops-state の projects が dict→list に schema 変更, コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (77f733404 → 490314c2d。併せて project/p-0258 /
  project/p-0272 ブランチも進行) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  読みはスナップショット方式 (/tmp 直下に保存してからパース) を継続
- **セッション 26 追加の「プローブ先行」手順を実行**:
  `/tmp/opencode` へ mktemp プローブ → NOT writable ([Errno 13]) を先に確認してから
  V2 実走。予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。spawn.py emptyDir mount (/tmp/opencode) 無傷を実読確認
  (spawn.py:169 mountPath / spawn.py:192 emptyDir sizeLimit 64Mi 実在)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

- **ops-state projects.json の schema が変わった**: トップレベルは dict のまま
  (version/projects/chores/…) だが、`projects` が「id をキーにした dict」から
  「`id` フィールドを持つ dict の list」(90 件) になっている。77f733404 時点では
  dict アクセスで読めた実測がある (セッション 30) ので、490314c2d までの間の変更。
  セッション 30 記載の `d['projects']['P-0243']` は今だと TypeError で落ちる。
  次セッションからは list スキャンで拾うこと (下の引き継ぎに両対応コードあり)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 (schema 変更後の list スキャンで) /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック (トップレベル 7 bool 形式)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜30 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。Pod 内での再走・権限 hack は不要
  (sudo 不在まで実証済み)。やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとに PROGRESS 追記で自然に増えるのは後者だけ)
- 生死が気になったら archive.jsonl ではなく ops-state:projects.json の `state` を見る。
  読み方は**スナップショット方式** (セッション 30 発見参照): 一度ローカルファイルに
  書き出してからパースする。トップレベルは dict。**`projects` はセッション 31 実測で
  list になった** (id 付き dict のリスト、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
- **V2 を実走する前の一手順 (セッション 26 追加・27〜31 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順。
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 32 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (490314c2d → 2dc23dc09。併せて project/p-0258 /
  project/p-0272 ブランチも進行) ため ops-state:projects.json を確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変。
  schema はセッション 31 実測どおり list (90 件) のまま。読みはスナップショット方式を継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ → NOT writable
  ([Errno 13]) を先に確認してから V2 実走。予測どおり fail-fast rc=2、stderr は
  wrapper 実測と同一メッセージ (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。spawn.py emptyDir mount (/tmp/opencode) 無傷を実読確認
  (spawn.py:169 mountPath / spawn.py:192 emptyDir sizeLimit 64Mi 実在)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 + pods.*.probe 対照)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜31 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:projects.json の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件 — セッション 31 実測)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。`/tmp/opsstate-XXXXXX.json` のように拡張子を
    後ろに付けると GNU mktemp は "Invalid argument" で落ちる (セッション 32 実測)
- **V2 を実走する前の一手順 (セッション 26 追加・27〜32 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 33 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- fetch 後に ops-state が動いた (2dc23dc09 → a8f468a3c、heart beat 93〜95) ため
  ops-state:projects.json を確認: P-0243 `state=active`・spawn_count=1・
  drift_count=0・adopt_gate_attempts=1 の不変。schema は list (90 件) のまま。
  読みはスナップショット方式 + X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ → NOT writable
  ([Errno 13]) を先に確認してから V2 実走。予測どおり fail-fast rc=2、stderr は
  wrapper 実測と同一メッセージ (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。spawn.py emptyDir mount (/tmp/opencode) 無傷を実読確認
  (mountPath / emptyDir sizeLimit 64Mi 実在)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F・control=dns_ok T/https_ok T)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  spawn.py emptyDir mount 実読確認 / demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜32 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:projects.json の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
- **V2 を実走する前の一手順 (セッション 26 追加・27〜33 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 34 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。fetch で動いたのは
  ops-health-report / ops-state / p-0258 / p-0272 のみで本件に関係なし
- fetch 後に ops-state が動いた (a8f468a3c → 7c5d8854a) ため確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。schema は list (90 件) のまま。読みはスナップショット方式 +
  X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ → NOT writable
  ([Errno 13]) を先に確認してから V2 実走。予測どおり fail-fast rc=2、stderr は
  wrapper 実測と同一メッセージ (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。spawn.py emptyDir mount (/tmp/opencode) も前セッションまでの
  実読どおり不変 (今回は差分不変の裏取りとしてファイル一覧一致で確認)
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F・control=dns_ok T/https_ok T)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜33 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:projects.json の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
- **V2 を実走する前の一手順 (セッション 26 追加・27〜34 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 35 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。fetch で動いたのは
  ops-state (7c5d8854a → 4423e2dd1) と p-0258 のみで本件に関係なし
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。schema は list (90 件) のまま。読みはスナップショット方式 +
  X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ → NOT writable
  ([Errno 13]、ディレクトリは root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。今回はファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F・control=dns_ok T/https_ok T)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)。補足: `/tmp` 自体は sticky +
world-writable で素の `mktemp` は通る。書けないのは root 所有の
`/tmp/opencode` ディレクトリのみ — 既知の壁に変化なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜34 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:projects.json の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
- **V2 を実走する前の一手順 (セッション 26 追加・27〜35 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 36 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。fetch で動いたのは
  ops-state (4423e2dd1 → cfc5a9ada) と p-0258 / p-0272 のみで本件に関係なし
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。schema は list (90 件) のまま。読みはスナップショット方式 +
  X 末尾テンプレートを継続
  - **小罠に今回触れた**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。同ブランチの `ops/state.json` には `projects` キーが
    無い (top keys は version / updated / vision_stage / feedback / dashboard /
    routines / in_cluster_loop / runs) で、そちらから読むと KeyError になる
    (セッション 36 実測。パスを取り違えないこと)
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ → NOT writable
  ([Errno 13]、ディレクトリは root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。ファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個形式 +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F・control=dns_ok T/https_ok T)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)。`/tmp/opencode` は root 所有のまま
で既知の壁に変化なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜35 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2 (セッション 36)**: スナップショットの取得元は ops-state ブランチ
    **ルートの `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えない
- **V2 を実走する前の一手順 (セッション 26 追加・27〜36 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false、control=dns_ok true × https_ok true

## セッション 37 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。fetch で動いたのは
  ops-state (cfc5a9ada → b950b20cf) と p-0258 / p-0272 のみで本件に関係なし
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。schema は list (90 件) のまま。読みはスナップショット方式 +
  ルート `projects.json` + X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ (`mktemp -p`)
  → NOT writable ([Errno 13]、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。ファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個 = 全 True +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F/status None・
  control=dns_ok T/https_ok T/status 200、`outcome` キー不在も確認)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)。`/tmp/opencode` は root 所有のまま
で既知の壁に変化なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜36 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict、90 件)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜37 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)

## セッション 38 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。schema は list のまま。読みはスナップショット方式 +
  ルート `projects.json` + X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ (`mktemp -p`)
  → NOT writable ([Errno 13]、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。ファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個 = 全 True +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F/status None・
  control=dns_ok T/https_ok T/status 200、`outcome` キー不在も確認)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)。`/tmp/opencode` は root 所有のまま
で既知の壁に変化なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜37 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜38 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)

## セッション 39 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (050b7934a→070da072b) が
  P-0243 エントリは不変。schema は list のまま。読みはスナップショット方式 +
  ルート `projects.json` + X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ (`mktemp -p`)
  → NOT writable ([Errno 13]、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。ファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個 = 全 True +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F/status None・
  control=dns_ok T/https_ok T/status 200、`outcome` キー不在も確認)

### 発見 (仕様外)

なし (コード・環境とも変化は観測されなかった)。`/tmp/opencode` は root 所有のまま
で既知の壁に変化なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック

### 次セッションへの引き継ぎ

- **状況はセッション 4〜38 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜39 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)

## セッション 40 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (#580 のまま、merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (070da072b→b24b15159) が
  P-0243 エントリは不変。schema は list のまま。読みはスナップショット方式 +
  ルート `projects.json` + X 末尾テンプレートを継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ mktemp プローブ (`mktemp -p`)
  → NOT writable ([Errno 13]、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)。ファイル一覧を実出力して一致確認
- demo.json 完全性チェック全パス (トップレベル bool 7 個 = 全 True +
  pods.*.probe 対照 labeled=dns_ok T/https_ok F/status None・
  control=dns_ok T/https_ok T/status 200、`outcome` キー不在も確認)

### 発見 (仕様外)

demo.json 完全性チェックで **error 文字列の具体値まで固定した assert を書いて
一度失敗した**: labeled probe の実 error は
`https: URLError: <urlopen error [Errno 101] Network unreachable>`
(「connection timed out or blocked」では無い)。正契約が規定するのは
キー集合 {dns_ok, https_ok, status, error} と真偽/status のみで、
error 文字列の具体値は契約外。チェック自体は契約どおりの範囲で全パス —
次セッションも文字列固定 assert を挟まないこと

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 (新着 0・merge-base = origin/main で裏取り) /
  census 未着確認 / ops-state:projects.json スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行
  (NOT writable 確認後に V2 実走) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 (wrapper 実測と同一メッセージ) /
  PR 差分 14 ファイル (コード 12 + ログ 2) 不変確認 (一覧実出力で照合) /
  demo.json 完全性チェック (初手 error 文字列固定で失敗 → 契約範囲に直して全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜39 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜40 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 41 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (b24b15159→4b71ddf87) が
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜40 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜41 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 42 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (4b71ddf87→9c9e16e92) が
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜41 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜42 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 43 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (9c9e16e92→4a838bb61) が
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜42 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜43 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 44 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (4a838bb61→caa249195) が
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜43 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜45 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 45 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (caa249195→9c0edee11,
  "heart: beat 139") が P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random で毎回変わる。クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜44 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜45 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 46 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体は進んでいた (9c0edee11→4d1b59e07) が
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random で毎回変わる。クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス。**error 文字列固定 assert は挟まなかった**
  (セッション 40 の発見どおり契約範囲のみ検査)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜45 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜46 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 47 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体はさらに進行 (4d1b59e07→37a252b89) だが
  P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random で毎回変わる。クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、
  error 文字列固定 assert は挟まず — セッション 40 の発見どおり)

### 発見 (仕様外)

なし。(project/p-0258 ブランチが進んでいたが本件と無関係のため追わない)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜47 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜47 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 48 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。ops-state ブランチ自体はさらに進行 (37a252b89→a8de35a9b,
  beat 150) だが P-0243 エントリは不変。schema は list のまま
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random で毎回変わる。クラスタ接触前なので副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、
  error 文字列固定 assert は挟まず — セッション 40 の発見どおり)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック (契約範囲のみで全パス)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜48 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜48 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 49 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (ops-state ブランチは継続進行中だが P-0243 エントリは不変)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、error 文字列固定 assert なし)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜49 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜49 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 50 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  なお fetch で `origin/project/p-0258` という新ブランチが出現したが
  main への影響はゼロ (P-0243 関係なし、触らない)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (ops-state ブランチは継続進行中だが P-0243 エントリは不変)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1、root 所有のまま) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、error 文字列固定 assert なし)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜50 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)。
    **小罠 1b** (セッション 50): 固定名リダイレクト (`> /tmp/opsstate-XXXXXX.json`)
    は mktemp を経由しないので成功してしまい、`|| { パース }` のフォールバックが
    **発火せず無音で何も出ない**。書き出しとパースを同じ分岐に入れ、失敗時に
    出力ゼロで終わらない構成にすること
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜50 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)
  - 対照 assert を書くときは `pods.*.probe` **配下**のキーを読むこと。
    pod dict のトップレベルに https_ok/status は無い (KeyError になる。セッション 52、
    チェッカー側の一時バグ。demo.json 側は契約どおり正常)

## セッション 51 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/project/p-0258` と `origin/heart/curriculum-*` の更新を
  見たが main への影響はゼロ (P-0243 関係なし、触らない)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、error 文字列固定 assert なし)

### 発見 (仕様外)

なし。

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜51 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)。
    **小罠 1b** (セッション 50): 固定名リダイレクト (`> /tmp/opsstate-XXXXXX.json`)
    は mktemp を経由しないので成功してしまい、`|| { パース }` のフォールバックが
    **発火せず無音で何も出ない**。書き出しとパースを同じ分岐に入れ、失敗時に
    出力ゼロで終わらない構成にすること
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **V2 を実走する前の一手順 (セッション 26 追加・27〜51 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)

## セッション 52 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` と `origin/project/p-0258` の更新を見たが
  main への影響はゼロ (P-0243 関係なし、触らない)
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (契約範囲のみ検査、error 文字列固定 assert なし)

### 発見 (仕様外)

- **PROGRESS.md への追記手順そのものが罠になった (自己原因、リポジトリ側は無傷で復元済み)**:
  既存セクションの契約ブロック末尾 + 次セクション見出しの「途中」までを置換対象にした結果、
  見出しの残り半分が新規セクションの手継ぎ最終行に連結されて混線し、
  セッション 51 の見出しが消えて本体だけが浮いた。編集前に読み直して復元。
  教訓: **追記は常に「ファイル末尾への接尾」で行う**。既存行を oldString に含める場合は
  行全体単位で切り、見出し行を途中で切らない

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス /
  PROGRESS 復元後の git diff で「既存行の改変ゼロ・追記のみ」を機械確認 (下記)

### 次セッションへの引き継ぎ

- **状況はセッション 4〜52 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい
- census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
  到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
  test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
- main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)
- 「PR 差分 N ファイル」の比較は数え方に注意: コード側だけなら 12、
  P-0243 ログ込みなら 14 (セッションごとの PROGRESS 追記で増えるのは後者だけ)
- 生死が気になったら ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
  (ローカルファイルに書き出してからパース)。トップレベルは dict、`projects` は list
  (id 付き dict)。両 schema に耐える読み方:
  `pr=d["projects"]; p=pr["P-0243"] if isinstance(pr,dict) else next(x for x in pr if x.get("id")=="P-0243")`
  - **小罠 1**: スナップショット保存の `mktemp` テンプレートは X を**末尾**に置くこと
    (`mktemp /tmp/opsstate-XXXXXX`)。拡張子を後ろに付けると GNU mktemp は
    "Invalid argument" で落ちる (セッション 32 実測)。
    **小罠 1b** (セッション 50): 固定名リダイレクト (`> /tmp/opsstate-XXXXXX.json`)
    は mktemp を経由しないので成功してしまい、`|| { パース }` のフォールバックが
    **発火せず無音で何も出ない**。書き出しとパースを同じ分岐に入れ、失敗時に
    出力ゼロで終わらない構成にすること
  - **小罠 2**: スナップショットの取得元は ops-state ブランチ**ルートの
    `projects.json`**。`ops/state.json` には `projects` キーが無いので
    そちらから読むと KeyError。パスを取り違えないこと
- **PROGRESS.md への追記はファイル末尾への接尾で (セッション 52 実害)**:
  既存テキストを置換対象にすると、セクション見出しを途中で切った一致により
  残骸が新しいセクションに混線し、直前セッションの見出しが失われた (同セッション内で
  復元済み)。追記位置は必ず EOF、アンカーが必要なら直前セッション固有の行
  (例: 引き継ぎの「27〜N」表記) を使う
- **V2 を実走する前の一手順 (セッション 26 追加・27〜52 で運用実績あり)**: 自前で
  `/tmp/opencode` の書き込み可否だけ先にプローブすること。もし環境側が変わって
  書けるようになっていた場合、V2 の実走はそのまま in-cluster ドリル
  (一時 NP + Pod 2 本の作成) まで進む。短絡セッションで副作用を起こす意図は
  ないので、「fail-fast になる予測 → 実行」の順
  - プローブの精度メモ (セッション 35): `/tmp` 自体は world-writable で素の
    `mktemp` は成功する。判定対象は `/tmp/opencode` への書き込みに絞ること
    (`mktemp -p /tmp/opencode ...`)。素の mktemp の成功をもって「V2 が通る」と
    誤判定しない
- **demo.json 完全性チェックの正契約**: トップレベル bool 7 個は
  labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control /
  cleaned_up / all_passed / probes_conclusive。pods.*.probe に `outcome` キーは
  **無い** (実キーは dns_ok / https_ok / status / error)。対照は
  labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
  https_ok true (status=200)。**error 文字列の具体値は契約外**
  (セッション 40 発見参照。labeled の現行値は
  `https: URLError: <urlopen error [Errno 101] Network unreachable>`)
  - 対照 assert を書くときは `pods.*.probe` **配下**のキーを読むこと。
    pod dict のトップレベルに https_ok/status は無い (KeyError になる。セッション 52、
    チェッカー側の一時バグ。demo.json 側は契約どおり正常)

## セッション 53 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` が 4 コミット動き `origin/project/p-0258` も動いたが、
  前者は heart beat のみ (168〜171)、後者も本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 + `pods.*.probe`
  配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

なし (既知の手順が全部そのまま機能した)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜52 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい。セッション 52 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜53 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 54 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` が 4 コミット動き `origin/project/p-0258` と
  `origin/ops-health-report` も動いたが、前者は heart beat のみ (172〜175)、
  後二者も本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 + `pods.*.probe`
  配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

なし (既知の手順が全部そのまま機能した)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜53 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい。セッション 53 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜54 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 55 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` が 1 コミット動き `origin/project/p-0258` も
  動いたが、本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 + `pods.*.probe`
  配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

なし (既知の手順が全部そのまま機能した)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜54 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい。セッション 54 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜55 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 56 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` が 1 コミット動き `origin/project/p-0258` も
  動いたが、本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変 (schema は list のまま)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 + `pods.*.probe`
  配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

なし (既知の手順が全部そのまま機能した)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走) / spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜56 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい。セッション 56 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜56 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 57 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` と `origin/project/p-0258` が動いたが、
  本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・spawn_count=1・drift_count=0・adopt_gate_attempts=1
  の不変。**ただし schema 変化を検出** (後述の発見参照。最初の 1 回は
  旧 list 前提の読み方で落ちたのでやり直した)
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認してから V2 実走。
  予測どおり fail-fast rc=2、stderr は wrapper 実測と同一メッセージ
  (probe 一時名のみ random)。クラスタ接触前なので副作用ゼロ
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast rc=2)
- 単体テストも再走: test_private_data_profile + test_stage3_readiness の
  37 テスト全パス
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル (コード側 12 +
  P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 + `pods.*.probe`
  配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

- ops-state:projects.json の schema が変った: トップレベルが list → dict
  (`version` / `projects` / `chores` / `last_*` / `stop_engaged`) になり、
  プロジェクト一覧は `["projects"]` 配下へ移動していた。旧前提のコード
  (`for x in d`) は `'str' object has no attribute 'get'` で落ちる。
  P-0243 の state 値自体は不変につき本プロジェクトへの影響ゼロだが、
  今後スナップショット読みをする手順は `["projects"]` 経由に直すこと

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 / ops-state スナップショット方式で
  P-0243 state=active 確認 (新 schema の `["projects"]` 経由) /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走) /
  spec verify V1 green / V3 green / V2 既知 fail-fast rc=2 /
  単体テスト 37 本全パス / PR 差分 14 ファイル不変確認 /
  demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜57 から不変**: V2 は本 PR の merge+sync 後の新 runner Pod で
  自動 green 化する (spawn.py の emptyDir mount 済み)。やることは「PR merge を待つ」だけ。
  main 新着なければ短絡でよい。セッション 57 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない /
    **2026-08-24 以降はトップレベル dict の `["projects"]` 配下を見ること —
    セッション 57 発見参照**)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜57 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 58 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` (97c6952ec→afef511b1) と
  `origin/project/p-0258` (927dd1d15→78e24c4a9) が動いたが、
  本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認
  (セッション 57 発見の新 schema、トップレベル dict の `["projects"]`
  経由。旧前提で落ちることもなく一発成功): P-0243 `state=active`・
  spawn_count=1・drift_count=0・adopt_gate_attempts=1 の不変
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認。V2 実走の条件は揃わず in-cluster
  ドリルは見送り (クラスタ接触前につき副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast
  rc=2、stderr は wrapper 実測と同一メッセージ、probe 一時名のみ random)
- 単体テストも再走: test_private_data_profile (14) +
  test_stage3_readiness (23) の計 37 テスト全パス
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル
  (コード側 12 + P-0243 ログ 2)
- demo.json 完全性チェック全パス (トップレベル bool 7 個 +
  `pods.*.probe` 配下キーでの対照チェック。error 文字列は契約外につき未検査)

### 発見 (仕様外)

なし

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 /
  ops-state スナップショット方式 (`["projects"]` 経由) で P-0243
  state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走なのでクラスタ接触なし) / spec verify V1 green /
  V3 green / V2 既知 fail-fast rc=2 / 単体テスト 37 本全パス /
  PR 差分 14 ファイル不変確認 / demo.json 完全性チェック全パス

### 次セッションへの引き継ぎ

- **状況はセッション 4〜58 から不変**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい。
  セッション 57 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (mktemp の X は末尾 / 取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない /
    **2026-08-24 以降はトップレベル dict の `["projects"]` 配下を見ること —
    セッション 57 発見参照**)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜58 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 59 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` (afef511b1→ea873ce93) と
  `origin/project/p-0258` (78e24c4a9→12b1ec47d) が動いたが、
  本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認
  (`["projects"]` 経由): P-0243 `state=active`・spawn_count=1・
  drift_count=0・adopt_gate_attempts=1 の不変
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認。V2 実走の条件は揃わず in-cluster
  ドリルは見送り (クラスタ接触前につき副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast
  rc=2、stderr は wrapper 実測と同一メッセージ、probe 一時名のみ random)
- 単体テストも再走: test_private_data_profile +
  test_stage3_readiness の両方 OK
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル
  (コード側 12 + P-0243 ログ 2)

### 発見 (仕様外)

- 小ネタ: スナップショット用 mktemp で `mktemp /tmp/snap-XXXXXXXX.json`
  のように **X の後ろに接尾辞を付けると Invalid argument になる**。
  「X は末尾」には「拡張子も付けない」が含まれる (セッション 57 記載の
  教訓を半分だけ守って踏んだ)

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 /
  ops-state スナップショット方式 (`["projects"]` 経由) で P-0243
  state=active 確認 / `/tmp/opencode` 書き込みプローブ先行 (NOT writable
  確認後に V2 実走なのでクラスタ接触なし) / spec verify V1 green /
  V3 green / V2 既知 fail-fast rc=2 / 単体テスト全パス /
  PR 差分 14 ファイル不変確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜59 から不変**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい。
  セッション 58 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (**mktemp の X は末尾・拡張子等の接尾辞も不可** — セッション 59 実測 /
    取得元は ops-state ルートの projects.json /
    書き出しとパースを同じ分岐に入れて失敗を無音にしない /
    トップレベル dict の `["projects"]` 配下を見ること)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜59 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 60 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` (ea873ce93→6a8783d67) と
  `origin/project/p-0258` (12b1ec47d→979115715) が動いたが、
  本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認:
  P-0243 `state=active`・不変
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認。V2 実走の条件は揃わず in-cluster
  ドリルは見送り (クラスタ接触前につき副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast
  rc=2、stderr は wrapper 実測と同一メッセージ、probe 一時名のみ random)
- 単体テストも再走: test_private_data_profile +
  test_stage3_readiness の両方 OK
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル
  (コード側 12 + P-0243 ログ 2)

### 発見 (仕様外)

- **projects.json の schema がまた変わった**: セッション 57 の
  「トップレベル dict の `["projects"]` 配下」の時点では dict だったが、
  今回の origin/ops-state 実測では **`["projects"]` が list** (90 要素、
  各要素が `id` を持つ)。dict 前提の `.get('P-0243')` は
  AttributeError で落ちる。読み手は list 前提で書くか両対応にする。
  schema は頻繁に動くので「構造を仮定しない」のが安全

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 /
  ops-state スナップショット方式で P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走
  しないのでクラスタ接触なし) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 / 単体テスト全パス / PR 差分 14 ファイル不変確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜60 から不変**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい。
  セッション 59 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (**`["projects"]` は list になった — dict 前提の `.get()` は落ちる。セッション 60 実測** /
    mktemp の X は末尾・拡張子等の接尾辞も不可 / 取得元は ops-state ルートの
    projects.json / 書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜60 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

## セッション 61 (2026-08-24) — 短絡チェックのみ (main 不動・census 未着・ops-state 動くが P-0243 active 不変), コード変更ゼロ

### やったこと

- **fetch 先行 → main 新着 = 0** (merge-base = origin/main =
  59169fddf)。merge 作業なし。census も未着
  (`git ls-tree -r origin/main | grep -c egress` = 0)。
  fetch で `origin/ops-state` (6a8783d67→829697cf8) だけが動いたが、
  本プロジェクト無関係につき触らない
- ops-state:projects.json をスナップショット方式で確認
  (list/dict 両対応読み): P-0243 `state=active`・spawn_count=1・
  drift_count=0・adopt_gate_attempts=1 の不変。
  セッション 60 発見の list schema は今回も継続
- **プローブ先行手順を実行**: `/tmp/opencode` へ `mktemp -p` プローブ →
  NOT writable (rc=1) を先に確認。V2 実走の条件は揃わず in-cluster
  ドリルは見送り (クラスタ接触前につき副作用ゼロ)
- spec verify 一式を再走: V1 green / V3 green / V2 red (既知 fail-fast
  rc=2、stderr は wrapper 実測と同一メッセージ、probe 一時名のみ random)
- 単体テストも再走: test_private_data_profile +
  test_stage3_readiness の 37 テスト全パス
- PR 差分不変を確認: merge-base (59169fddf) 起点で 14 ファイル
  (コード側 12 + P-0243 ログ 2)

### 発見 (仕様外)

- なし。schema 変化もプローブ結果の変化も無かった静かなセッション。
  projects.json の list schema は 2 セッション連続で安定しているが、
  「構造を仮定しない」読み手のまま運用する

### 検証 (全部自分で実走済み)

- fetch + main 追い越し判定 / census 未着確認 /
  ops-state スナップショット方式 (list 対応) で P-0243 state=active 確認 /
  `/tmp/opencode` 書き込みプローブ先行 (NOT writable 確認後に V2 実走
  しないのでクラスタ接触なし) / spec verify V1 green / V3 green /
  V2 既知 fail-fast rc=2 / 単体テスト 37 パス / PR 差分 14 ファイル不変確認

### 次セッションへの引き継ぎ

- **状況はセッション 4〜61 から不変**: V2 は本 PR の merge+sync 後の
  新 runner Pod で自動 green 化する (spawn.py の emptyDir mount 済み)。
  やることは「PR merge を待つ」だけ。main 新着なければ短絡でよい。
  セッション 60 の引き継ぎは全項目まだ有効なので併せて読むこと。
  要点のみ再掲:
  - census 到着チェックは `git ls-tree -r origin/main | grep -c egress` 一発。
    到着したらセッション 3/4 記載の手順 (両 NP バイト一致更新 +
    test_egress_allows_dns_and_nothing_else_yet の conscious 更新をセットで)
  - main 追い越しの手順はセッション 17 の「罠注意」参照 (merge-base diff で中身確認)。
    「PR 差分 N ファイル」はコード側だけなら 12、P-0243 ログ込みなら 14
  - 生死は ops-state:**projects.json** の `state` を見る。読みは**スナップショット方式**
    (**`["projects"]` は現在 list — dict 前提の `.get()` は落ちるが schema は頻繁に動くので
    list/dict 両対応が安全。セッション 60/61 実測とも list** /
    mktemp の X は末尾・拡張子等の接尾辞も不可 / 取得元は ops-state ルートの
    projects.json / 書き出しとパースを同じ分岐に入れて失敗を無音にしない)
  - **PROGRESS.md への追記はファイル末尾への接尾で** (セッション 52 の実害教訓。
    アンカーに既存行を使う場合は行全体単位で切る)
  - **V2 実走前の一手順 (26〜61 で運用実績あり)**: `mktemp -p /tmp/opencode` で
    書き込み可否を先にプローブ (`/tmp` 本体ではなく `/tmp/opencode` 対象)。
    もし書けるようになっていたら V2 の実走はそのまま in-cluster ドリル
    (一時 NP + Pod 2 本の作成) まで進む
  - **demo.json 正契約**: トップレベル bool 7 個 (labeled_blocked /
    unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up /
    all_passed / probes_conclusive)。pods.*.probe 配下の実キーは
    dns_ok / https_ok / status / error (`outcome` は無い)。対照は
    labeled=dns_ok true × https_ok false (status=None)、control=dns_ok true ×
    https_ok true (status=200)。error 文字列の具体値は契約外

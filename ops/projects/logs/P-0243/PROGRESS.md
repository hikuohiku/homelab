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

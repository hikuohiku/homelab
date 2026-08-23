# P-0163 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。

## セッション 1 — 2026-08-23: 検査ツール本体 + unittest (verify 1・2 を green 化)

### やったこと

- `ops/tools/syncthing_acceptance.py` 新設 (stdlib のみ)。`check` サブコマンドが
  検査リストを `{name, required, status, detail}` で集約して表示し exit code を返す。
  必須 7 項目: identity-files / device-id-format / self-device-declared /
  folder-paths / pvc-rw / restic-coverage / (restic マニフェスト読めないときのみ unknown)。
  任意 2 項目: gui-health / tailnet-sync。**FAIL は必須/任意を問わず rc=1、
  UNKNOWN は単独では許容だが --strict で rc=1** (「応答が異常」のような確定的な
  否定情報を見逃さないため。到達できないことだけは落とさない — version_watch 流儀)
- device ID 導出は syncthing 本家 lib/protocol/deviceid.go + luhn.go の移植。
  sha256(DER) → base32 (52 文字, pad除去) → 4 個の luhn32 検査数字挿入 (56 文字)
  → 7 文字区切り (8 グループ)。**ゴールデンベクトルは本家のテストから移植**
  (`P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2`、旧 52 文字形式・
  タイポ修正 0→O/1→I/8→B も受ける)。正規化は canonical_device_id() に集約
- 空回し演習 (DoD 2) は `exercise` サブコマンド。ダミーフォルダ acceptance-dummy の
  登録 → 書き込み → rescan 収束待ち → 読み戻し → restic 静的カバレッジ確認 →
  **後始末は finally で必ず実行** (フォルダ削除 + ディレクトリ削除)。
  exercise の exit code は UNKNOWN も不合格 (演習は確定させるものなので)
- `ops/tests/test_syncthing_acceptance.py` 新設 (37 テスト、ネットワーク注入で無通信)。
  実マニフェストに対する restic-coverage テストは cronjob の意図しない変更の drift 検知も兼ねる

### verify 自己実測

```
$ test -f ops/tools/syncthing_acceptance.py            # rc=0
$ python3 -m unittest ops.tests.test_syncthing_acceptance   # Ran 37 tests ... OK
```

CI 相当も通過確認: `python3 -m unittest discover -s ops/tests -t .` → Ran 279 tests OK。

### 分かったこと / 実測

1. **この runner 環境からクラスタ Service に直接届く** (2026-08-23 実測):
   `getent hosts syncthing.syncthing.svc` → 10.43.228.154、
   `GET http://syncthing.syncthing.svc:8384/rest/noauth/health` → 200 `{"status": "OK"}`、
   TCP 22000 to syncthing-sync ClusterIP (10.43.6.132) 接続成功。
   つまり wrapper 実行でも gui-health/tailnet-sync は「不明」でなく green に出る。
   --strict は restic マニフェストさえ渡せばこの環境から成立する
2. **cert.pem/config.xml の配置には 2 通りの可能性があり未確定**: repo 内で
   restic cronjob の exclude は `config/index-v2` 表記なのに backup コメントは
   裸の `config.xml` を列挙している。ツールは flat (PVC 直下) / nested (config/ 配下)
   を自動判別するが、docs に「置き場所」を書くときは実物で確定すること
   (PROJECT.md 前提の「PVC root 直下・mountPath 実測」なら flat)。ここを推測で書かないこと
3. fixture 証明書は手組み DER ではなく公開ルート CA (Amazon Root CA 3) を採用。
   最初に ASN.1 手組みで作ったら OpenSSL パーサーに何度拒否され時間を溶かした
   (乱数ストリーム 128 バイト制限で s が切れていた等)。runner 環境に openssl/
   cryptography は無いが、システム CA バンドルから真正な自己署名証明書を
   取り込む方が早くて確実。期待値 (b52/canonical) は ssl+hashlib+base64 の
   独立経路で算出し、luhn 部だけ本家ベクトルで担保する構成にしたので
   「自分の実装の焼き直し」テストになっていない
4. exercise CLI は稼働中 syncthing の設定を変える (専用フォルダの追加/削除のみとはいえ)。
   動作試験は必ず死んだポート宛てで (`--gui-url http://127.0.0.1:9`)。

### 次のセッションへの一言

残りは verify 3 の `docs/syncthing-migration.md`。含めるべきは:
LXC 101 での tar 1 コマンド (`tar -C /var/lib/syncthing -czf - .` とパス要確認注記)、
取り出したファイルの置き場所 (上記 2 の確定待ち。所有権 1000:1000 への言及)、
検証コマンド 1 回 (check --strict + exercise の起動方法。in-cluster 一時 Job か
port-forward かを決めてコマンド形に落とす)、失敗時ロールバック
(**LXC 101 は合格まで停止しない** / 追加した config 差分を抜いて新規インストールに戻す)。
罠: `/tmp/opencode` はこの環境では書けない (Permission denied、`/tmp/st-cert` を使用)。
ruff バイナリも無い (py_compile で代用済み)。

## セッション 2 — 2026-08-23: 移行台本 docs/syncthing-migration.md (verify 3 を green 化、3 項目すべて green)

### やったこと

- `docs/syncthing-migration.md` 新設 (395 行)。構成: 全体像 → 前提 → 手順 A (LXC 101 で
  tar 1 コマンド + パス要確認の事前確認) → 手順 B (PVC 配置、コピペブロック) → 手順 C
  (受け入れ検証 1 回) → 手順 D (片付け) → ロールバック → check の読み方対処表 → 合格後 → 既知の死角
- **検証は in-cluster Job に決めた** (port-forward 不採用)。理由: pvc-rw / exercise は PVC への
  実書き込みを要し、port-forward は REST しか通さない。Job 内ならツール既定の Service DNS
  (--gui-url / --sync-addr) がそのまま正になり引数不要。restic マニフェストは ConfigMap に同梱して
  --strict を成立させる (Job 外実行だと restic-coverage が「不明」になるため)
- **配置スクリプト** (migrate Pod 内で実行): layout を推測でなく実物から検出 (現 cert.pem の位置に
  合わせる。セッション 1 の「flat/nested 確定待ち」は配置時実測で解消する設計にした)、旧 HOME
  (`/var/lib/syncthing`) → 新 HOME への folder path 張り替え、`.pristine` 待避 (mv なので所有権ごと)
  = ロールバック点 + LAYOUT 目印ファイル、identity 3 点 + https 証明書を cp -a、同期フォルダ中身は
  identity/再生成物以外全部、chown -R 1000:1000、鍵 600 保証
- **新発見: GUI バインド修正が必須**。LXC の config.xml は `<address>127.0.0.1:8384` が既定。
  このままだと移行直後に Service/probe が到達できず再起動ループになる。sed で 0.0.0.0:8384 に
  張り替えるステップを入れた (docs の gui-health 対処表にも反映)

### verify 自己実測

```
$ test -f docs/syncthing-migration.md && grep -q 'LXC 101' docs/syncthing-migration.md   # rc=0
$ test -f ops/tools/syncthing_acceptance.py                                              # rc=0
$ python3 -m unittest ops.tests.test_syncthing_acceptance     # Ran 37 tests ... OK
$ python3 -m unittest discover -s ops/tests -t .              # Ran 279 tests ... OK (CI 相当)
```

### 台本の空回し試験 (合成環境での実測)

doc 内の配置スクリプト・ロールバックスクリプトを抽出し、合成ディレクトリ (旧 HOME flat 構成 +
同期フォルダ 2 個 vs 新規インストール nested 構成) で往復試験した:

- placement rc=0: nested 検出、path 張り替え (/var/lib/syncthing/Sync → /var/syncthing/Sync)、
  GUI バインド修正、apikey 維持 (exercise が config.xml から読むので必須)、フォルダデータ複製、
  鍵 600 維持、.pristine に新規インストール側 identity+index-v2+LAYOUT 待避
- rollback rc=0: 新規インストール状態へ完全復元 (.pristine 解消まで)。**過程でバグ 2 件発見→修正**:
  (1) mkdir が rm より前だと作ったものを消し直す (順序変更)、(2) LAYOUT 目印を消さないと
  rmdir が落ちる (rm 追加)。どちらも doc 修正済みで、この記録以降の doc は通過版
- YAML は PyYAML safe_load、スクリプトは sh -n + 実行で検証

### 分かったこと / 実測

1. **runner 環境は非 root (uid 10001)** — chown 1000:1000 は空回しでは stub に置換して試験した。
   実機では migrate Pod が root + CHOWN/FOWNER/DAC_OVERRIDE (docs/backup.md 復元試験の教訓どおり)
   で動く前提。migrate Pod のイメージは python:3.12-alpine (inventory 監視済み・node に cache 済み)
2. **検証 Job を uid/gid 1000 で動かす**ことで pvc-rw が「本番 Pod と同じ権限で読み書きできるか」の
  判定になる (root で動かすと所有権問題を見逃す)
3. 移行直後からピアとの同期は始まりうる (device ID 不変のため接続は自動)。よって台本は rollout 直後に
   検証する順序。不合格でもロールバックすれば LXC 101 が正であり続け、ピア側データは失われない
4. `kubectl exec` で stdin スクリプトを流すには `-i` が必要 (無いと heredoc が届かない)
5. GUI ログイン資格情報は移行後**旧 LXC 101 のもの**になる (config.xml ごと移るため)。docs に記載済み

### 発見 (スコープ外、curriculum 拾い待ち)

- 台本の migrate Pod / acceptance Job の YAML は**実機での空回しがまだ** (合成環境と構文検査のみ)。
  初回適用時に軽微な手直しが出る可能性はあるが、全操作可逆なので再試行で吸収できる。docs にも明記した
- exercise は稼働中インスタンスの設定を API 経由で一時変える (ダミーフォルダ追加/削除のみ)。「Git → CI →
  ArgoCD を通さない変更」に見えうるが、spec DoD 2 が明示要求する演習であり cleanup 付き。レビューで
  突かれたら spec を根拠に弁護する

### 次のセッションへの一言

verify 3 項目とも green のはず (wrapper 実測を優先)。次はレビュー対応の可能性が高い。残っている未確定:
(1) 台本 YAML の実機空回し未実施 — レビュー指摘が出たらここが濃厚。(2) `.pristine` 待避は「2 回目以降の
実行では触らない」設計だが、ロールバック→再配置を繰り返すと pristine が初回のまま保たれる点は意図的
(戻り先は常に「今日の新規インストール」)。(3) 手順 A の tar パス `/var/lib/syncthing` は依然要確認
(人間が preflight grep で確かめる形に逃がした)。罠はセッション 1 引き継ぎ: `/tmp/opencode` 書けない、
ruff 無し (py_compile/sh -n 代用)、一時ファイルは mktemp。

## セッション 3 — 2026-08-23: レビュー指摘の解消 — exercise cleanup の実機必敗バグを修正

### やったこと

レビュー指摘 (exercise の後始末が実機で必ず失敗し最終ゲートが永久に合格しない) の解消:

1. **再現テストを先に追加** (`StFolderMkdirApi`): RecordingApi を継承し、`POST /rest/db/scan`
   受信時に `local_dir/.stfolder` を mkdir する fake (本物は初回 scan で .stfolder を掘る。
   素の fake はファイルシステムに触れないためこのバグを捉えられなかった)。
   現行コードで回すと 2 テストとも exercise-cleanup=unknown で red — 指摘どおり再現
2. **修正**: run_exercise 内 cleanup() の `marker.unlink() + local_dir.rmdir()` を
   `shutil.rmtree(local_dir)` に置換 (stdlib、制約違反なし)。detail 文言も
   「ダミーディレクトリを削除 (.stfolder 含む)」に変更。red → green を確認
3. **テスト 2 本追加** (37→39):
   - happy path で .stfolder 掘られても全体合格・ディレクトリ跡形なく消える
     (`stfolder_created` フラグの assert で「再現条件が成立していた」ことを保証 — 空振り防止)
   - rescan タイムアウト (exit 1) の経路でも .stfolder ごと消えて cleanup は PASS
4. **docs/syncthing-migration.md 更新 2 箇所**: 手順 C の合格条件に「.stfolder も含めて丸ごと消す」を追記。
   読み方表の exercise-* 行は「cleanup UNKNOWN → GUI から手動削除」という誤誘導を修正 —
   フォルダ登録の DELETE 自体は成功しているので、「folder 削除に失敗」という detail のときだけ
   GUI からの手動削除、「ダミーディレクトリ削除に失敗」なら残骸確認のみ、という切替に書き換えた

### verify 自己実測

```
$ test -f ops/tools/syncthing_acceptance.py                                          # rc=0
$ python3 -m unittest ops.tests.test_syncthing_acceptance     # Ran 39 tests ... OK
$ test -f docs/syncthing-migration.md && grep -q 'LXC 101' docs/syncthing-migration.md  # rc=0
$ python3 -m unittest discover -s ops/tests -t .              # Ran 281 tests ... OK (CI 相当)
```

### 分かったこと / 実測

1. 指摘の再現手順どおり、scan 受信時に .stfolder を掘る fake で run_exercise を回すと
   全項目 pass に対し exercise-cleanup のみ unknown ([Errno 39] Directory not empty)、
   exit code=1。rmtree 化で解消
2. cleanup の UNKNOWN 判定は「folder 登録の DELETE 失敗」と「ローカルディレクトリ削除の
   失敗」を 1 項目に集約している。今回の修正で後者はほぼ起きなくなる (残るのは権限異常のみ)
   ので、今後 cleanup が UNKNOWN なら DELETE 側の失敗が第一疑い。docs の対処表にも反映済み
3. rmtree は marker.unlink() の役割も吸収する (.stfolder 以外に何が増えても消える)。
   一方「local_dir が最初から無い」ケースは FileNotFoundError → 従来同様 UNKNOWN 扱い
   (挙動を変えないようにした)

### 発見 (スコープ外)

- なし (指摘範囲のみ修正)。台本 YAML の実機空回し未実施は前セッションからの持ち越しで
  変わらず — レビューで次に突かれるとすればここ

### 次のセッションへの一言

レビュー指摘 1 点は解消済み (red→green 実測、39 テスト)。verify 3 項目は green のはず
(wrapper 実測を優先)。未確定の持ち越し: (1) 台本 YAML (migrate Pod / acceptance Job) の
実機空回しがまだ — 合成環境での往復試験と構文検査 (sh -n, PyYAML safe_load) のみ。
(2) 手順 A の tar パス `/var/lib/syncthing` は要確認のまま (人間の preflight grep に逃がしている)。
罠: `/tmp/opencode` は書けない (Permission denied)、ruff 無し (py_compile 代用)、
一時ファイルは mktemp。RecordingApi を継承した fake を作るときは request() の
オーバーライドで super() を必ず呼ぶこと (calls 記録が途切れる)。

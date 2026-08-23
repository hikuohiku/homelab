# P-0270 PROGRESS

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

**実測の証拠はここに貼る。** DoD (3) の CI green → ArgoCD Healthy と DoD (4) の DNS
ブロック実測は verify が見張っていない。コマンドと実出力を貼らなかった分は、
存在しなかったことになる。

## セッションログ

### s1 (2026-08-23) — initializer

- PROJECT.md 作成。verify 3 項目とも failing を実測 (rc=1)。実装は未着手。

### s2 (2026-08-23) — apps/adguard 新設 + inventory 登録 (verify #1/#3 → green)

**やったこと** (commit 5ce63135b):

- `apps/adguard/` を 8 ファイルで新設 (syncthing P-0047 型):
  Deployment + PVC(5Gi) + seed ConfigMap + tailscale LB Service +
  restic backup(3:20 JST)/retention(日曜 5:10 JST) CronJob + ExternalSecret 2 本 +
  application.yaml。`apps/kustomization.yaml` / CLAUDE.md / apps/README.md へ配線済み
- `ops/inventory.json` に `adguard` (image, v0.107.79) と `adguard-restic-image`
  (0.19.1) を追加。`check_version_sync.py` の restic GROUPS に6ファイル目、
  `check_credential_map.py` の DECLARED_SECRET_TARGETS に adguard-restic-{backup-,}credentials を追加
- イメージ pin は実測: Docker Hub タグ一覧と GitHub Releases latest の両方で
  **v0.107.79** (2026-08-18 リリース) が最新安定版。v0.108.0-b.* は beta なので追わない

**verify 実測**:

```
$ test -f apps/adguard/kustomization.yaml && kubectl kustomize apps/adguard >/dev/null; echo $?
0                                    # ← verify #1 green
$ python3 -c "import json;i=json.load(open('ops/inventory.json'))['targets'];assert any('adguard' in t['id'] for t in i)"
(通過)                               # ← verify #3 green
$ kubectl kustomize apps/adguard | ... # 8 objects: ConfigMap/Service/PVC/Deployment/CronJob x2/ExternalSecret x2
$ python3 -m unittest discover -s ops/tests -t .   # Ran 440 tests, OK
$ python3 ops/validate.py                          # 0 error (warning 11 件は既存)
$ python3 ops/check_version_sync.py && python3 ops/check_credential_map.py   # とも ok
```

**設計の要点** (次のセッションはここを読めば再調査不要):

- **seed ConfigMap 方式**: 初回起動時だけ initContainer (本体と同じ adguard イメージ)
  が `conf/AdGuardHome.yaml` を PVC へコピーする。理由 (1) 種が無いとウィザード完了後に
  UI が 0.0.0.0:80 へ移り、Service/probe が見ている 3000 から UI が消える。
  理由 (2) AdGuard DNS filter を同梱し「入れたのに何もブロックされない」を避ける。
  ConfigMap を conf に直接 mount しないのは read-only になり UI からの保存が壊れるため
- **probe**: GET / on :3000。セットアップ前後どちらでも redirect 先が最終的に 200 になるので
  httpGet probe が両フェーズで通る (kubelet は redirect を追う)。users 空 (= 未セットアップ)
  でも DNS(:53) とフィルタは seed 設定で動く想定 — **DoD (4) の dig 実測は人間のウィザード
  完了を待たずにできるはず**。ダメならその事実を記録して人間手順と合わせる
- **expose 設定値 (DoD 4 の記録材料)**: Service `adguard` (ns adguard),
  type LoadBalancer + loadBalancerClass tailscale, annotation
  `tailscale.com/hostname: adguard`, ports 53/tcp+53/udp+3000。
  端末の DNS 先 = `adguard.<tailnet domain>`、UI = `http://adguard.<tailnet>:3000`
- restic リポジトリ suffix は `adguard`。backup CronJob は conf/+work/ を subPath で
  readOnly mount して丸ごと取得 (除外なし — AGH の work/ は小さく torn copy を
  気にするより単純な方が良い判断)。securityContext は syncthing 型 (root + DAC_READ_SEARCH)

**発見 (仕様外なので触らない)**:

- `ops/check_app_list_sync.py` は **CI に配線されていない** (.github/workflows/ci.yml の
  ops job は個別列挙で glob ループ化 T-0157 未適用)。しかも main 現在で drift 6 件
  (version-watcher / nats / autopilot-core が CLAUDE.md・apps/README.md に無言)。
  PROJECT.md 設計方針 1 は「CI が記載を要求する」と書いているが現状は要求されない。
  今回は adguard 分だけ自分で揃えた。drift 6 件は curriculum が拾うべき別論点

**次のセッションへの一言**:

1. 残 verify は **#2 のみ**: `python3 -m unittest ops.tests.test_adguard_manifest` を
   新規作成する。assert 対象の案: レンダリング 8 オブジェクトの kind/name、deployment の
   image が `adguard/adguardhome:v0.107.79` で 2 箇所 (initContainer+本体)、memory limits
   が無いこと、CronJob schedule 2 本、Service の loadBalancerClass=tailscale と hostname
   annotation、inventory 2 エントリの file 実在。ネットワークに出ない合成 fixture 流儀
   (test_syncthing_acceptance.py 参照) を守ること
2. DoD (3)(4) は merge 後: PR → CI green → merge → ArgoCD Synced/Healthy 確認 →
   使い捨て Job (ArgoCD 管理外、消したことをここに書く) で
   `doubleclick.net` がブロック応答・`example.org` が正常解決を実測してコマンドと出力を貼る
3. イメージを上げるときは deployment 内 2 箇所 + inventory current を同じ PR で。
   GROUPS の restic 6 ファイル一致も崩さないこと

### s3 (2026-08-23) — test_adguard_manifest 新設 (verify #2 → green、**verify 全項目 green**)

**やったこと** (commit af5914ff5):

- `ops/tests/test_adguard_manifest.py` を新設 (40 テスト)。s2 の案どおり
  レンダリング 8 オブジェクト / image 2 箇所同値 / memory limits 無し (deployment
  init+本体・CronJob 両方) / schedule 2 本 / tailscale LB + hostname annotation /
  inventory 2 エントリの file 実在に加え、下記も固定した:
  - PVC の `Prune=false` annotation (ロールバック節のデータ消失回避が外れたら落ちる)
  - seed ConfigMap の中身を YAML としてパース: `http.address=0.0.0.0:3000` 固定
    (ウィザード後の UI :80 移動防止)、filter 有効・upstream/bootstrap 有無、
    **`users` を書かない**こと (管理ユーザーは人間が作る)
  - backup CronJob = append-only 鍵 (`adguard-restic-backup-credentials`) /
    retention = 削除鍵 (`adguard-restic-credentials`)、repository suffix `:adguard`、
    readOnly mount、retention は PVC mount 無し、保持世代 `7/4/6`
  - ExternalSecret: doppler ClusterSecretStore、backup だけ
    `B2_ACCOUNT_{ID,KEY}_APPEND_ONLY` を引く
  - application.yaml (path/ns/CreateNamespace/automated) と apps/kustomization.yaml
    への配線、ディレクトリ内 yaml の未配線検出

**実測**:

```
$ python3 -m unittest ops.tests.test_adguard_manifest -v     # Ran 40 tests, OK ← verify #2 green
$ python3 -m unittest discover -s ops/tests -t .             # Ran 480 tests, OK
$ kubectl kustomize apps/adguard >/dev/null; echo $?         # 0 ← verify #1 green (再確認)
$ python3 -c "...inventory..." ; echo $?                     # 0 ← verify #3 green (再確認)
$ python3 ops/validate.py                                    # 0 error (warning 11 件は既存)
$ python3 ops/check_version_sync.py && python3 ops/check_credential_map.py   # とも ok
```

**テスト設計の要点** (次のセッションはここを読めば再調査不要):

- **kubectl/kustomize を呼ばない**: CI の ops job には入っていない (ci.yml 実測、
  test_backup_coverage.py 冒頭にも同じ事情あり)。代わりに PyYAML で resources 6 ファイルを
  直接パースし「連結 = レンダリング相当」として検査。ネットワーク不出
- **バージョン番号は anchor にしなかった**: 正しい引き上げ手順 (deployment 2 箇所 +
  inventory current を同一 PR) ならテストを触らず通る。代わりに「initContainer と本体と
  inventory current が同値」「restic 2 CronJob 同値」の一致性不変条件で部分更新を落とす。
  タグ形式の v 付きのみチェック (Docker Hub 実測に基づく inventory note の前提)
- **変異試験済み**: image タグ片側変更 → FAILED / Prune=true 変更 → FAILED /
  schedule 衝突時刻変更 → FAILED をそれぞれ実測してから戻した。「通って当たり前」の
  空振りテストではない

**次のセッションへの一言**:

1. **コード側の verify は完了** (全 3 項目 green)。残りは DoD (3)(4) のみ:
   PR → CI green → merge → ArgoCD Synced/Healthy 確認 → 使い捨て Job (ArgoCD 管理外、
   消したことをここに書く) で `doubleclick.net` ブロック / `example.org` 正常解決を実測し、
   コマンドと出力をこのファイルに貼る。expose 設定値は s2 の記録を使える
2. 人間のウィザード (:3000, users 空) を待たずに dig 実測できるはず (seed 設定で DNS+
   filter は動く)。ダメならその事実と人間手順を記録して止まること — 完了宣言はしない
3. テストを壊すような manifest 変更をするときは test_adguard_manifest も同一 PR で
   直す (schedule・保持世代・鍵分離はすべて意図的な選択として pin してある)

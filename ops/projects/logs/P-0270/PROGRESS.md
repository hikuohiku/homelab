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

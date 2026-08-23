# P-0270 — tailnet 全端末に DNS 広告除去を開通させる (AdGuard Home, syncthing 実績型)

## 目的

seeds H6 (homelab 新機能) の直接消費で、stalled の P-0228 を乗り越えた再提案。tailnet 広域の
DNS 広告除去は人間の全端末の毎日に効く、VISION 段階 2 の成果として直に数えられる利用者価値。
P-0228 当時に不明だった資源余裕は今や実測で前置きでき (substrate.md: allocatable 4 vCPU /
11.7GiB に対し requests 合計約 1.2 CPU / 2.6 GiB)、小さい常駐 1 つの余地が数字で示せる。
さらに #49 型の「入れたのに誰も見てない」静放置を最初から塞ぐ — inventory.json 登録と
version-watcher 対象化を完成条件に含める (P-0047 が潰した棚卸しの穴を新規アプリで繰り返さない)。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0270` の checkout でリポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f apps/adguard/kustomization.yaml && kubectl kustomize apps/adguard >/dev/null`
  — apps/adguard/ が存在し、kustomize レンダリングが壊れていないこと。
    実測 rc=1 (apps/adguard/ 自体が未存在)。
- [ ] `python3 -m unittest ops.tests.test_adguard_manifest -v`
  — adguard manifest の構造検査テストが存在し green であること。
    実測 rc=1 (モジュール自体が無い)。
- [ ] `python3 -c "import json;i=json.load(open('ops/inventory.json'))['targets'];assert any('adguard' in t['id'] for t in i), 'inventory に未登録'"`
  — adguard イメージが version-watcher の網に入っていること。
    実測 rc=1 (AssertionError 'inventory に未登録')。

**verify は DoD の下限であって DoD そのものではない。** 3 本ともファイル/モジュールの存在しか
見ない。**DoD (3) の CI green → ArgoCD Healthy 同期と、DoD (4) の DNS ブロック実測は verify が
一切見張っていない** — `PROGRESS.md` とプロジェクトログにコマンドと実出力をそのまま貼ることが
唯一の証拠になる。貼らなかった分は存在しなかったことになる。

## 設計方針

前提は initializer が 2026-08-23 に実読・実測した。調べ直さなくてよい。

1. **手本は `apps/syncthing/` 一式** (T-0138 新設 + P-0047 backup 型)。新規アプリの登録面:
   `apps/adguard/application.yaml` を作り `apps/kustomization.yaml` の `resources` に足せば
   App of Apps root (`apps/apps.yaml`, path: apps, automated prune/selfHeal) が拾う。このとき
   CI の `ops/check_app_list_sync.py` が **CLAUDE.md と apps/README.md へのアプリ名記載を要求する**
   ので両方に足す。root は prune:true — render から消すとクラスタからも消える (manifest-diff が歯止め)。
2. **PVC を置くと `ops/tests/test_backup_coverage.py` が発火する** (「PVC を宣言する app ディレクトリは
   restic CronJob を kustomization 登録して持つこと」— syncthing で潰した穴の機械検査)。型:
   `restic-external-secret.yaml` 2 本 (backup = append-only 鍵 / retention = 削除鍵。雛形は
   `apps/vaultwarden/restic-external-secret.yaml`) + `restic-backup-cronjob.yaml`
   (backup/retention 2 CronJob)。ClusterSecretStore `doppler` は cluster スコープなので新 namespace
   からそのまま引け、Doppler キーは既存共用 (人間待ちゼロ)。schedule は JST 評価 (node01
   timeZone)。既存占有: backup 2:45/3:10/3:30/3:40/3:55、retention 日曜午前4時台 — 衝突しない時刻を
   選ぶ。保持世代は既存 3 本と同じ `--keep-daily 7 --keep-weekly 4 --keep-monthly 6`。
   リポジトリパスは bucket 後ろの suffix `adguard`。
3. **Deployment**: 公式イメージ `adguard/adguardhome` を現時点の最新安定版にバージョン pin
   (タグ形式は Docker Hub 実測で確認して書く)。**memory limits は付けない** (substrate 規則)、
   CPU limits のみ。requests は最小値から。probe を付ける (初期セットアップ前後で通るエンドポイントを
   選ぶこと)。AdGuard の DHCP 機能は使わない (k8s では無意味)。
4. **tailnet 公開は service-tailnet 型**: `type: LoadBalancer` + `loadBalancerClass: tailscale` +
   `tailscale.com/hostname` annotation が L3 DNAT で TCP/UDP 全プロトコル転送する
   (syncthing-sync 実績) — DNS 53/tcp+udp 向き。管理 UI も tailnet 到達のみで外部公開しない
   (Service の型選択で担保)。expose の設定値 (hostname annotation・ポート) をプロジェクトログに
   記録すること。
5. **inventory.json 登録** (verify 3): targets に追加。必須フィールドは id/kind/name/current/file/
   match/upstream/policy/note (`check_inventory` が非空と file 実在を検査)。version-watcher が
   このファイルを実行時に読むので、登録自体が網入り。単一ファイル pin なので
   `ops/check_version_sync.py` の GROUPS 追加は不要 (GROUPS は二重管理 pin 用)。mirrors は
   付けない。
6. **実測の順序 (DoD 3・4)**: PR → CI green → merge → ArgoCD で Synced/Healthy を確認してから次へ。
   in-cluster からの使い捨て Job (busybox nslookup/dig 等) で 広告ドメイン = ブロック応答・
   通常ドメイン = 正常解決 の両方を実測し、コマンドと実出力をそのまま貼る。使い捨て Job/PVC は
   ArgoCD 管理外で作って消し、消したことを PROGRESS.md に書く。

### ロールバック

追加のみの変更なので revert PR 1 本で apps/adguard 全体が消えるだけ。ただし root の prune:true により
merge 後に PVC に溜めたデータも消える — データが価値を持つようになる (設定済みフィルタ等) 以降の
revert は PVC 消滅を伴うことを PR 本文に書くこと。

## やらないこと

- **人間の端末側への DNS 設定適用 (opt-in)**。tailnet 側の expose 設定値と端末ごとの設定手順を
  ログに残すまで。強制も一括変更もしない (判断は人間の拒否権/選択)。
- **既存アプリの変更**。backup schedule も保持世代も credential も触らない。他アプリの pin 更新も
  しない (1 PR 1 論点)。
- **`.github/workflows/` の変更**。新規 job を足すと ruleset の必須チェック追加が人間専有で
  止まる。既存 discover (`ops/tests`) に寄せる。
- **管理 UI の外部公開・Ingress・TLS 証明書運用**。tailnet 内到達のみ。
- **`ops/rules.json` / `ops/backlog.json` / `ops/state.json` / `ops/journal/` / CHARTER・VISION・
  `ops/memory/` の更新**。heart が直接 main に push する領域と不可侵層 (CLAUDE.md)。
- **B2 側の設定** (バケット・鍵の capability 変更)。管理コンソール操作は人間専有。
- **検証用の一時 Job/PVC を `apps/` に commit すること**。ArgoCD 管理に入ると prune と immutable を踏む。

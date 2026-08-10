# P-0047 — syncthing の backup / 復元試験 実出力 (2026-08-10)

「試したことのないバックアップは、バックアップではありません」(issue #56, 2026-08-05) に従い、
`syncthing-data` の restic バックアップを実際に取り、使い捨て PVC へ復元して原本と突き合わせた。
**要約ではなくコマンドと実出力をそのまま貼る。** ここが DoD (2)(3) の唯一の証拠。

## 実施条件

- 日時: 2026-08-10 08:38–08:55 UTC (JST 17:38–17:55)。日次 backup のスケジュール
  (3:55 JST) とは重ならない時間帯
- 本番 PVC (`syncthing-data`) は **readOnly マウントでしか触っていない**。syncthing Pod
  (`syncthing-747879bfcf-vl85r`, 3d18h) は停止せず稼働したまま
- manifest はまだ merge されていないため、`apps/syncthing/restic-external-secret.yaml` と
  `apps/syncthing/restic-backup-cronjob.yaml` を `kubectl apply` で一時的に投入して実測し、
  **終わってから全部削除した**(下の「後片付け」)。ArgoCD の tracking ラベルが付かないので
  prune の対象にはならず、既存アプリの同期には影響しない
- 復元は **append-only 鍵 (`syncthing-restic-backup-credentials`) だけ**で完結した。
  削除権限を持つ `syncthing-restic-credentials` は復元に持ち出していない

## 1. 原本を数える (稼働中 Pod の中身)

```
$ kubectl exec -n syncthing syncthing-747879bfcf-vl85r -- sh -c 'echo "=== find -type f | wc -l ==="; find /var/syncthing -type f | wc -l; echo "=== du -sh ==="; du -sh /var/syncthing'
=== find -type f | wc -l ===
10
=== du -sh ===
120.0K	/var/syncthing
```

```
$ kubectl exec -n syncthing syncthing-747879bfcf-vl85r -- sh -c 'find /var/syncthing -type f -exec ls -la {} \;'
-rw-r--r--    1 1000     1000           623 Aug  6 09:17 /var/syncthing/config/cert.pem
-rw-r--r--    1 1000     1000           778 Aug  6 09:17 /var/syncthing/config/https-cert.pem
-rw-------    1 1000     1000             0 Aug  6 14:31 /var/syncthing/config/syncthing.lock
-rw-------    1 1000     1000           119 Aug  6 09:17 /var/syncthing/config/key.pem
-rw-r--r--    1 1000     1000         32768 Aug 10 01:18 /var/syncthing/config/index-v2/main.db
-rw-r--r--    1 1000     1000         32768 Aug 10 01:19 /var/syncthing/config/index-v2/main.db-shm
-rw-r--r--    1 1000     1000          4152 Aug 10 01:18 /var/syncthing/config/index-v2/main.db-wal
-rw-------    1 1000     1000           227 Aug  6 09:17 /var/syncthing/config/https-key.pem
-rw-------    1 1000     1000          6590 Aug  6 09:17 /var/syncthing/config/config.xml.v0
-rw-------    1 1000     1000          6590 Aug  6 09:17 /var/syncthing/config/config.xml
```

**原本 sha256（突き合わせの基準。2026-08-10T08:38:06Z 時点）**

```
$ kubectl exec -n syncthing syncthing-747879bfcf-vl85r -- sh -c 'cd /var/syncthing && find . -type f | sort | xargs sha256sum'
baf46bf58dd3491ff8adb9353cf9310d4e53d9d77df08ac8d9d98b21fa56b34b  ./config/cert.pem
82a6a1fba8a33c3640374c2f8c465467e87bb708f68163a6851199d8fede8acc  ./config/config.xml
82a6a1fba8a33c3640374c2f8c465467e87bb708f68163a6851199d8fede8acc  ./config/config.xml.v0
1f6267de260f6da5593661371c0a8ec6b06b73baab1ef815386dc66320061331  ./config/https-cert.pem
405b1eab3d11611801be89adddcc727d7b2c0b562492fba8fb1f11af96037af3  ./config/https-key.pem
fdb3f7de1cc7e24fa8b1eedf4d2e711a0ecbba8e769f76969e1ca408592979b3  ./config/index-v2/main.db
d770e999ae1d210118829bbfeb7030de5d6728540fa9128daf1b1caaf980820f  ./config/index-v2/main.db-shm
7f843f8d27dad57b6b11473dcfdd473faab1dee44fa2917a0f6a0dea26186e9e  ./config/index-v2/main.db-wal
fde36f4a3d09afdd9ac08ab0a45952bf20d6dd4536b974169023e82b5e2e36df  ./config/key.pem
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  ./config/syncthing.lock
```

`--exclude` はここで見た**実ファイル名**から決めた (綴りを推測で書くと黙って効かない):
`config/index-v2/` (main.db / main.db-shm / main.db-wal / .tmp) と `config/syncthing.lock`。
→ backup 対象は **10 - 4 = 6 ファイル**になるはず。

> T-0140 (旧 LXC 101 からの実データ移行) が未着手のため、この時点の PVC の中身は syncthing
> 自身の identity と設定だけ。同期フォルダの実ファイルはまだ 1 つも無い。**復元一致を確認した
> 規模は 6 ファイル・14.577 KiB であり、実データが流れ込んだ後の規模での再試験は別途要る。**

## 2. ExternalSecret が同期すること

```
$ kubectl apply -f apps/syncthing/restic-external-secret.yaml
externalsecret.external-secrets.io/syncthing-restic-credentials created
externalsecret.external-secrets.io/syncthing-restic-backup-credentials created

$ kubectl get externalsecret -n syncthing
NAME                                  STORETYPE            STORE     REFRESH INTERVAL   STATUS         READY   LAST SYNC
syncthing-restic-backup-credentials   ClusterSecretStore   doppler   1h                 SecretSynced   True    8s
syncthing-restic-credentials          ClusterSecretStore   doppler   1h                 SecretSynced   True    10s
```

Doppler への新規登録は要らなかった (既存キーをそのまま参照。`*_APPEND_ONLY` を含む)。

## 3. backup の実測 (DoD 2)

```
$ kubectl apply -f apps/syncthing/restic-backup-cronjob.yaml
cronjob.batch/syncthing-restic-backup created
cronjob.batch/syncthing-restic-retention created

$ kubectl get cronjob -n syncthing
NAME                         SCHEDULE     TIMEZONE   SUSPEND   ACTIVE   LAST SCHEDULE   AGE
syncthing-restic-backup      55 3 * * *   <none>     False     0        <none>          0s
syncthing-restic-retention   50 4 * * 0   <none>     False     0        <none>          0s

$ kubectl create job -n syncthing syncthing-restic-backup-manual-20260810 --from=cronjob/syncthing-restic-backup
job.batch/syncthing-restic-backup-manual-20260810 created

$ kubectl wait --for=condition=complete job/syncthing-restic-backup-manual-20260810 -n syncthing --timeout=360s
job.batch/syncthing-restic-backup-manual-20260810 condition met

$ kubectl get job -n syncthing syncthing-restic-backup-manual-20260810
NAME                                      STATUS     COMPLETIONS   DURATION   AGE
syncthing-restic-backup-manual-20260810   Complete   1/1           17s        17s

$ kubectl logs -n syncthing job/syncthing-restic-backup-manual-20260810
created restic repository 89037dc407 at b2:hikuohiku-homelab:syncthing

Please note that knowledge of your password is required to access
the repository. Losing your password means that your data is
irrecoverably lost.
no parent snapshot found, will read all files

Files:           6 new,     0 changed,     0 unmodified
Dirs:            3 new,     0 changed,     0 unmodified
Added to the repository: 11.068 KiB (5.006 KiB stored)

processed 6 files, 14.577 KiB in 0:06
snapshot 8608514d saved
```

| 項目 | 結果 |
|---|---|
| rc | 0 (`Complete 1/1`) |
| 所要時間 | **17 秒** (Job 全体。restic 本体は 6 秒) |
| リポジトリ | `b2:hikuohiku-homelab:syncthing` を **append-only 鍵で新規 init できた** (ID `89037dc407`) |
| スナップショット | `8608514d` |
| 対象 | 6 files / 14.577 KiB (原本 10 ファイルから除外 4 を引いた数と一致) |
| 残留 lock | **無し** (次項の `restic snapshots` が待たされずに返っている。stale lock があれば警告が出る) |

## 4. 復元の実測 (DoD 3)

使い捨て PVC `syncthing-restore-drill` (local-path, 1Gi) と Job を `kubectl apply` で作った
(`apps/` には commit しない。ArgoCD 管理外の使い捨て)。実際に使った manifest:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: syncthing-restore-drill
  namespace: syncthing
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-path
  resources:
    requests:
      storage: 1Gi
---
apiVersion: batch/v1
kind: Job
metadata:
  name: syncthing-restore-drill-20260810
  namespace: syncthing
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    spec:
      automountServiceAccountToken: false
      restartPolicy: Never
      containers:
        - name: restic-restore
          image: restic/restic:0.19.1
          command:
            - sh
            - -c
            - |
              set -eu
              restic snapshots
              restic ls -l latest
              restic restore latest --target /restore
              find /restore -type f | wc -l
              ls -la /restore/mnt/syncthing-data/config
              cd /restore/mnt/syncthing-data && find . -type f | sort | xargs sha256sum
          env:
            # すべて syncthing-restic-backup-credentials (append-only) から引く。
            # RESTIC_REPOSITORY: "b2:$(RESTIC_B2_BUCKET):syncthing"
            ...
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
              add: ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
          volumeMounts:
            - name: restore
              mountPath: /restore
      volumes:
        - name: restore
          persistentVolumeClaim:
            claimName: syncthing-restore-drill
```

```
$ kubectl wait --for=condition=complete job/syncthing-restore-drill-20260810 -n syncthing --timeout=180s
job.batch/syncthing-restore-drill-20260810 condition met

$ kubectl get job -n syncthing syncthing-restore-drill-20260810
NAME                               STATUS     COMPLETIONS   DURATION   AGE
syncthing-restore-drill-20260810   Complete   1/1           27s        27s

$ kubectl logs -n syncthing job/syncthing-restore-drill-20260810
=== restic snapshots ===
ID        Time                 Host                                           Tags        Paths                Size
-------------------------------------------------------------------------------------------------------------------------
8608514d  2026-08-10 08:43:23  syncthing-restic-backup-manual-20260810-hxc7p              /mnt/syncthing-data  14.577 KiB
-------------------------------------------------------------------------------------------------------------------------
Timestamps shown in UTC
1 snapshots
=== restic ls -l latest ===
snapshot 8608514d of [/mnt/syncthing-data] at 2026-08-10 08:43:23.432761271 +0000 UTC by root@syncthing-restic-backup-manual-20260810-hxc7p filtered by []:
drwxr-xr-x     0     0      0 2026-08-10 08:43:16 /mnt
drwxrwxrwx  1000  1000      0 2026-08-06 09:17:21 /mnt/syncthing-data
drwx------  1000  1000      0 2026-08-06 14:31:51 /mnt/syncthing-data/config
-rw-r--r--  1000  1000    623 2026-08-06 09:17:21 /mnt/syncthing-data/config/cert.pem
-rw-------  1000  1000   6590 2026-08-06 09:17:21 /mnt/syncthing-data/config/config.xml
-rw-------  1000  1000   6590 2026-08-06 09:17:21 /mnt/syncthing-data/config/config.xml.v0
-rw-r--r--  1000  1000    778 2026-08-06 09:17:21 /mnt/syncthing-data/config/https-cert.pem
-rw-------  1000  1000    227 2026-08-06 09:17:21 /mnt/syncthing-data/config/https-key.pem
-rw-------  1000  1000    119 2026-08-06 09:17:21 /mnt/syncthing-data/config/key.pem
=== restic restore latest --target /restore ===
restoring snapshot 8608514d of [/mnt/syncthing-data] at 2026-08-10 08:43:23.432761271 +0000 UTC by root@syncthing-restic-backup-manual-20260810-hxc7p to /restore
Summary: Restored 9 files/dirs (14.577 KiB) in 0:00
=== restored: find -type f | wc -l ===
6
=== restored: ls -la ===
total 40
drwx------    2 1000     1000          4096 Aug  6 14:31 .
drwxrwxrwx    3 1000     1000          4096 Aug  6 09:17 ..
-rw-r--r--    1 1000     1000           623 Aug  6 09:17 cert.pem
-rw-------    1 1000     1000          6590 Aug  6 09:17 config.xml
-rw-------    1 1000     1000          6590 Aug  6 09:17 config.xml.v0
-rw-r--r--    1 1000     1000           778 Aug  6 09:17 https-cert.pem
-rw-------    1 1000     1000           227 Aug  6 09:17 https-key.pem
-rw-------    1 1000     1000           119 Aug  6 09:17 key.pem
=== restored: sha256sum ===
baf46bf58dd3491ff8adb9353cf9310d4e53d9d77df08ac8d9d98b21fa56b34b  ./config/cert.pem
82a6a1fba8a33c3640374c2f8c465467e87bb708f68163a6851199d8fede8acc  ./config/config.xml
82a6a1fba8a33c3640374c2f8c465467e87bb708f68163a6851199d8fede8acc  ./config/config.xml.v0
1f6267de260f6da5593661371c0a8ec6b06b73baab1ef815386dc66320061331  ./config/https-cert.pem
405b1eab3d11611801be89adddcc727d7b2c0b562492fba8fb1f11af96037af3  ./config/https-key.pem
fde36f4a3d09afdd9ac08ab0a45952bf20d6dd4536b974169023e82b5e2e36df  ./config/key.pem
```

## 5. 突き合わせ

| 対象 | 原本 (手順 1) | 復元 (手順 4) | 判定 |
|---|---|---|---|
| `config/cert.pem` | `baf46bf5…6b34b` | `baf46bf5…6b34b` | **一致** |
| `config/key.pem` | `fde36f4a…e36df` | `fde36f4a…e36df` | **一致** |
| `config/config.xml` | `82a6a1fb…8acc` | `82a6a1fb…8acc` | **一致** |
| `config/config.xml.v0` | `82a6a1fb…8acc` | `82a6a1fb…8acc` | **一致** |
| `config/https-cert.pem` | `1f6267de…1331` | `1f6267de…1331` | **一致** |
| `config/https-key.pem` | `405b1eab…7af3` | `405b1eab…7af3` | **一致** |
| ファイル数 | 10 (うち除外 4) | 6 | **`restic ls latest` の 6 件と一致** |
| 所有権 / パーミッション | `1000:1000`, config は `0700` | 同左 | **一致** |

**代表ファイル 6 本すべて sha256 完全一致。** ファイル数は PROJECT.md の指示どおり
**スナップショット自身の件数 (`restic ls -l latest` の 6 件)** と復元結果 (6) を突き合わせている。
原本の 10 件との差 4 件は意図した `--exclude` (index-v2 の 3 ファイル + syncthing.lock)。

**稼働中の書き込みによるズレは今回は出なかった。** 根拠は「backup 対象の 6 ファイルは
mtime が 2026-08-06 09:17 で止まっており、稼働中の syncthing が書き続けているのは除外した
index-v2 の 3 ファイルだけ (mtime 2026-08-10 01:18–01:19)」であること。裏返すと、
**実データ移行 (T-0140) の後は同期フォルダが常時書き換わるので、次の復元試験では
原本と復元のファイル数がズレうる。そのときズレたこと自体は事故ではない**
(`restic ls latest` と復元結果が一致していれば backup は正しい)。

`config.xml` と `config.xml.v0` が同じハッシュなのは、v0 が初回起動時に作られたバックアップ
コピーで内容がまだ同一だから (異常ではない)。

## 6. 踏んだ罠 — 復元 Job には CHOWN と FOWNER が要る

backup 側 (`DAC_READ_SEARCH` だけ) と同じ securityContext を復元にも流用したところ 2 回落ちた。
**同じ内容が `docs/backup.md` の「coder workspace home（完了、2026-08-07、T-0117）」に
既に書いてあった** (「CHOWN/FOWNER/DAC_OVERRIDE + クリーンアップを初回実装から織り込んだ」)。
先に読んでいれば 2 回の失敗は要らなかった。

1 回目 — `drop: ["ALL"]` のみ:

```
ignoring error for /mnt/syncthing-data/config/cert.pem: lchown /restore/mnt/syncthing-data/config/cert.pem: operation not permitted
（同様に 8 件）
Summary: Restored 7 / 9 files/dirs (14.577 KiB / 14.577 KiB) in 0:00
Fatal: There were 8 errors
```

2 回目 — `add: ["CHOWN"]` を足した:

```
Summary: Restored 7 / 9 files/dirs (14.577 KiB / 14.577 KiB) in 0:00
ignoring error for /mnt/syncthing-data/config/cert.pem: failed to restore timestamp of "/restore/mnt/syncthing-data/config/cert.pem": operation not permitted
（同様に 8 件）
Fatal: There were 8 errors
```

理由: restic は復元先の所有権を `1000:1000` に戻すので `CHOWN` が要り、chown した**後**は
root であっても「所有者ではない」ため `utimensat` に `FOWNER` が要る。**どちらの失敗でも
ファイルの中身自体は 14.577 KiB 全部書けている** (`Restored 7 / 9` は所有権を戻せなかった
dir/file を失敗に数えた結果) が、restic は `Fatal` で終わるので Job としては失敗になる。
3 回目に `CHOWN` / `FOWNER` / `DAC_OVERRIDE` を足して初めて `Restored 9 files/dirs` で完走した。

**次に誰かが復元するときは、この 3 つを付けた Job から始めること。**

## 7. 後片付け

実測に使ったものは全部削除し、namespace を実施前の状態に戻した。

```
$ kubectl delete job syncthing-restore-drill-20260810 syncthing-restic-backup-manual-20260810 -n syncthing
job.batch "syncthing-restore-drill-20260810" deleted from syncthing namespace
job.batch "syncthing-restic-backup-manual-20260810" deleted from syncthing namespace

$ kubectl delete pvc syncthing-restore-drill -n syncthing
persistentvolumeclaim "syncthing-restore-drill" deleted from syncthing namespace

$ kubectl delete -f apps/syncthing/restic-backup-cronjob.yaml
cronjob.batch "syncthing-restic-backup" deleted from syncthing namespace
cronjob.batch "syncthing-restic-retention" deleted from syncthing namespace

$ kubectl delete -f apps/syncthing/restic-external-secret.yaml
externalsecret.external-secrets.io "syncthing-restic-credentials" deleted from syncthing namespace
externalsecret.external-secrets.io "syncthing-restic-backup-credentials" deleted from syncthing namespace

$ kubectl get externalsecret,cronjob,job,pvc -n syncthing
NAME                                   STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   VOLUMEATTRIBUTESCLASS   AGE
persistentvolumeclaim/syncthing-data   Bound    pvc-bcfae5c5-b0bb-43e3-aacd-579b862f8b44   20Gi       RWO            local-path     <unset>                 3d23h
```

**残ったもの 2 つ**(どちらも意図的、または消せない):

1. **B2 のリポジトリ `b2:hikuohiku-homelab:syncthing` とスナップショット `8608514d`**。
   append-only 鍵では消せないので残す (5.006 KiB。放置してよい)。merge 後の初回 backup は
   このリポジトリに追記され、`restic init` は走らない (`restic snapshots || restic init`)。
2. **Secret `syncthing-restic-credentials` / `syncthing-restic-backup-credentials`**。
   ExternalSecret の `deletionPolicy: Retain` により残り、かつ `autopilot-writer` は
   syncthing namespace の secrets に `get`/`list`/`delete` いずれの権限も持たないため消せない
   (`Error from server (Forbidden)`)。**merge 後にこれが同期を壊さないことを実測で確認済み**:

```
$ kubectl apply -f apps/syncthing/restic-external-secret.yaml     # merge 後に起きることの再現
externalsecret.external-secrets.io/syncthing-restic-credentials created
externalsecret.external-secrets.io/syncthing-restic-backup-credentials created

$ kubectl wait --for=condition=Ready externalsecret/syncthing-restic-credentials externalsecret/syncthing-restic-backup-credentials -n syncthing --timeout=90s
externalsecret.external-secrets.io/syncthing-restic-credentials condition met
externalsecret.external-secrets.io/syncthing-restic-backup-credentials condition met

$ kubectl get externalsecret -n syncthing
NAME                                  STORETYPE            STORE     REFRESH INTERVAL   STATUS         READY   LAST SYNC
syncthing-restic-backup-credentials   ClusterSecretStore   doppler   1h                 SecretSynced   True    1s
syncthing-restic-credentials          ClusterSecretStore   doppler   1h                 SecretSynced   True    2s
```

   残っていた Secret を ESO がそのまま引き取り、2 秒で `SecretSynced` / `Ready=True` になった。
   確認後この ExternalSecret 2 本もまた削除して実施前の状態に戻してある。

## 8. merge 後に確認すること

この実測は merge 前の `kubectl apply` で行ったので、**ArgoCD 経由で同じものが生えることは
まだ確認していない**。merge 後に以下を見ること。

```
kubectl get cronjob -n syncthing          # syncthing-restic-backup / -retention の 2 本
kubectl get externalsecret -n syncthing   # 2 本とも SecretSynced / Ready=True
kubectl get job -n syncthing              # 翌 3:55 JST 以降、日次 backup が Complete していること
```

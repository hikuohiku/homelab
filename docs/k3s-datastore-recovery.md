# k3s 状態ストアの復元手順

`apps/k3s-backup/restic-backup-cronjob.yaml` が毎日 02:30 JST に B2 の
`b2:$RESTIC_B2_BUCKET:k3s-datastore` へ入れているものを、node01 全損から戻す手順。
掬っているのは 2 つだけ。

| restic の中のパス | 実体 |
|---|---|
| `/staging/state.db` | `/var/lib/rancher/k3s/server/db/state.db` の一貫コピー (kine/sqlite) |
| `/mnt/k3s-server/token` | `/var/lib/rancher/k3s/server/token` (サーバートークン) |

**トークンが要る理由**: データストアの中の bootstrap データ (クラスタ CA の鍵など) は
トークン由来の鍵で暗号化されている。`state.db` だけ戻しても新品の k3s では開かない。
トークンは nix にも Doppler にも pin されておらず、node01 上で生成された値しか無い。

## どちらの経路で戻すか

- **クラスタにしか無い記録が無いなら、戻さない。** node01 を作り直し、`apps/apps.yaml` の
  App of Apps に再同期させ、アプリのデータは各アプリの restic リポジトリから戻す方が速く、
  実績もある (`docs/backup.md` の復元試験)。
- **クラスタにしか無い記録があるなら、戻す。**
  `docs/design/state-out-of-git/architecture.md` Phase 4 以降、プロジェクトの記録
  (`Project` CR) は git に無く、これが唯一の実体になる。

## 順序

1. **age 鍵を確保して node01 を建て直す。** `docs/sops-recovery.md` → `terraform/proxmox` で
   VM を作り直す。ここまでは k3s の状態ストアと無関係。
2. **k3s を止める。** 初回起動で新しい `state.db`・新しいトークン・新しい CA が
   生成されているので、そのまま上書きしてはいけない。

   ```
   systemctl stop k3s
   ```

3. **restic から取り出す** (別マシンでも node01 上でもよい。`RESTIC_PASSWORD` /
   `RESTIC_B2_BUCKET` / `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` は Doppler の `homelab/prd`)。

   ```
   restic -r "b2:$RESTIC_B2_BUCKET:k3s-datastore" snapshots
   restic -r "b2:$RESTIC_B2_BUCKET:k3s-datastore" restore latest --target /restore
   ```

4. **新規生成されたものを退避してから置き換える。** `tls/` と `cred/` を残すと、
   データストア側の bootstrap データと食い違って k3s が起動を拒む。消さずに待避する。

   ```
   cd /var/lib/rancher/k3s/server
   mv db db.new && mv tls tls.new && mv cred cred.new
   mkdir db
   install -m 0600 /restore/staging/state.db        db/state.db
   install -m 0600 /restore/mnt/k3s-server/token    token
   ```

   `state.db-wal` / `state.db-shm` は**戻さない**。バックアップは WAL を取り込んだ後の
   一貫コピーなので、古い WAL を持ち込むと壊れる。

5. **起動して確認する。**

   ```
   systemctl start k3s
   kubectl get nodes
   kubectl get applications -n argocd
   ```

6. **アプリのデータを各 restic リポジトリから戻す。** 状態ストアが持っているのは
   k8s のオブジェクトだけで、PVC の中身は入っていない。手順は `docs/backup.md`。

## 未確認

**この手順は実機で試していない** (作成時点で復元試験の記録なし)。特に次の 2 点は机上:

- 手順 4 の「`tls/` `cred/` を待避すれば、復元したトークンとデータストアから k3s が
  CA を再構成する」— k3s の bootstrap の挙動に依存する。
- `type: File` でマウントしている `/var/lib/rancher/k3s/server/token` が実在すること。
  存在しなければ backup Job は Pod が起動できず `activeDeadlineSeconds` で失敗する
  (黙って成功はしない)。

`docs/backup.md` の復元試験 (T-0071) と同じく、使い捨ての VM で 1 度通すまでは
「バックアップがある」とだけ言い、「復元できる」とは言わないこと。

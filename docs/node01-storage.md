# node01 ストレージ拡張

node01 (qemu/113) の root filesystem は `virtio0` → `/dev/vda3`（GPT / ext4）。
2026-08-04 に 50 GiB から 256 GiB へ、VM・k3s を止めずに拡張した。
`local-path` の PVC はこの filesystem を共有するため、PVC の要求容量は実容量を予約しない。

## オンライン拡張

1. `terraform/proxmox/vm-nixos.tf` の `disk.size` を増やし、`just plan` が node01 の
   **in-place update** だけで、再作成・停止を含まないことを確認する。
2. `just apply` で Proxmox の `virtio0` を拡張する。
3. `kubectl debug node/nixos --image=ubuntu:24.04 --profile=sysadmin` の一時 Pod で
   `cloud-guest-utils` を導入し、`growpart /host/dev/vda 3` を実行する。
4. `chroot /host /run/current-system/sw/bin/resize2fs /dev/vda3` で mounted ext4 を
   オンライン拡張する。完了した debug Pod は削除する。
5. `lsblk`・`df -hT /`、`kubectl get nodes`、全 Pod の Ready を確認する。

パーティション番号や filesystem が異なる場合は、この手順を使わず先に `lsblk -f` で確認する。
Kubernetes の `ephemeral-storage` は guest の拡張後もしばらく旧容量を表示することがある。
これは local-path PVC の実容量には影響しないため、可用性を優先して k3s を再起動せず
`df -hT /` を正として確認する。

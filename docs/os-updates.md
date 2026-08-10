# 土台（NixOS / k3s / カーネル）の更新手順

このリポジトリの更新は 2 層に分かれている。

- **アプリ層**（`apps/` のコンテナタグと Helm chart）: `ops/inventory.json` の 39 対象のうち 37。
  ArgoCD が Git から自動で追従するので、PR を出してマージすれば終わる。
- **土台層**（この文書）: node01 の OS そのもの。カーネル・systemd・**k3s** は
  `nix/images/proxmox-cloud/flake.lock` の `nixpkgs` が決めている。ArgoCD は届かない。

土台は `policy: manual` で、誰も上げなければ据え置かれる。アプリ層で同じ構造が事故になったのが
vaultwarden 1.36.0 の放置（#49、クライアント同期が全停止）だった。土台側でそれを繰り返さないために、
腐敗の検知を CI に置き（`ops/check_flake_freshness.py`）、更新の一本道をここに書く。

---

## 0. 先に知っておくこと — 差し替えには経路が 2 つあり、既定は image ではない

| | (A) in-place（**既定**） | (B) image 差し替え（DR・再プロビジョニング） |
|---|---|---|
| やること | node01 上で `nixos-rebuild` を打つ | リリース発行 → `nixos_image_version` 更新 → `terraform apply` |
| VM | 作り直さない | **`replace_triggered_by` で再作成される** |
| `local-path` PVC | 残る | **失われる**（restic からの復元が前提。`docs/backup.md`） |
| 今使えるか | 使える（人間が 1 コマンド） | **使えない。T-0107（pveproxy 証明書の SAN 不一致）で `terraform apply` は禁止中** |

(B) が VM 再作成になるのは、`terraform/proxmox/vm-nixos.tf` の
`lifecycle { replace_triggered_by = [proxmox_download_file.nixos_image.id] }` が
`terraform/proxmox/nixos-image.tf` の image ダウンロードを参照しているため。
**つまり image 経路は「通常の OS 更新」ではなく DR の経路である。** 通常の更新は (A) を使う。

以下 1〜5 は (A) の一本道。(B) は §6 に別に書く。

---

## 1. flake を更新する（器の仕事）

autopilot の実行環境には `nix` が無い（`ops/CHARTER.md` §5.2）。`flake.lock` を手で書くこともできない
（`narHash` は nix でしか計算できず、でっち上げれば `nix flake check` が NAR hash mismatch で落ちる）。
そのため更新は GitHub Actions の中でやる: `.github/workflows/nixos-image.yml` の `update` job。

**起動のしかた**（`workflow_dispatch` は器の token では 403 なので使えない）:

- main 以外のブランチで `nix/images/proxmox-cloud/**` かこの workflow 自身に触って push する。
- または `repository_dispatch`（この workflow が main に入っている場合のみ。
  repository_dispatch は default ブランチの workflow 定義しか実行しない）:

  ```bash
  curl -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/hikuohiku/homelab/dispatches \
    -d '{"event_type":"flake-update"}'
  ```

`update` job は **リポジトリへ push しない**。CI からブランチへ push すると worker の未 push commit と
非 fast-forward で衝突するため、更新後の `flake.lock` を**ログに base64 1 行で出す**。

**ログから lock を取り出して commit する**（run id は
`GET /repos/hikuohiku/homelab/actions/runs?branch=<branch>` → job id → `.../actions/jobs/<id>/logs`。
REST で平文のまま取れる）:

```bash
# ログ中の P0055_FLAKE_LOCK_BASE64_BEGIN / END に挟まれた 1 行を渡す
printf '%s' "<base64>" | base64 -d > nix/images/proxmox-cloud/flake.lock
git diff -- nix/images/proxmox-cloud/flake.lock   # 4 input の rev と lastModified が動いたか目で見る
```

同じ PR で `ops/inventory.json` の `nixpkgs` の `current`（= 新しい locked rev）・`upstream_rev`・
`last_checked` も更新する。**忘れると CI が落ちる**（`ops/check_flake_freshness.py` が
`current` と lock の rev の一致を見ている）。

## 2. 何がどこまで動くのかを確認する

`update` job は更新の前後で以下を `nix eval` して出す（ログの `P0055 before …` / `P0055 after …`）。

- `config.services.k3s.package.version` — **k3s**
- `config.boot.kernelPackages.kernel.version` — カーネル
- `config.system.nixos.version` — NixOS

`nix/images/proxmox-cloud/configuration.nix` は `services.k3s` の**バージョンを pin していない**
（`enable` / `role` / `extraFlags` のみ）。つまり **nixpkgs を動かすと k3s が黙って動く**。
2026-08-05 の更新では k3s 1.34.2 → 1.35.6 のマイナー更新がこの経路で起きた（#146 / T-0062）。

**マイナーを跨ぐなら、跨がせないこと。** 特に **k3s 1.36 系は containerd 2.x を要求する**
（1.35 が containerd 1.x をサポートする最終版）。跨ぎそうなら同じ PR で
`services.k3s.package` を明示 pin して現状の挙動を保ち、ホップ自体は別の変更として立てる。
土台の更新を定常業務にするとは、**k3s のホップを nixpkgs 更新の副作用ではなく明示の決定にする**ことでもある。

before→after が分かったら、**k3s とカーネルのリリースノートを現在版から目標版まで読む**
（`ops/CHARTER.md` §4）。読んだ結果は変更の記録に残す。

## 3. 実際にビルドする（CI）

lock を commit して push すると、同じ workflow の `build` job がその lock そのものをビルドする。

- `nixosConfigurations.proxmox-cloud.config.system.build.toplevel`
  — **これが (A) で node01 が実際に切り替わる先**。ここが通らなければ (A) は打てない。
- `packages.x86_64-linux.qcow2` — (B) 用の image（`release-image.yml` と同じ経路）。

sha256・バイト数・所要時間・closure サイズをログに出す。実測値は
`ops/projects/logs/P-0055/image-build.md` に記録している。

`ci.yml` の `nix` job は `nix flake check --no-build`（**評価のみ**）のままにしてある。
ビルドは重く、全 PR に載せると数十分になるため。

## 4. 人間に渡す手作業 — コマンド 1 回

器は node01 の**ホスト OS には届かない**（autopilot の Pod は node01 上で動いているが、
ホストではない）。ここから先だけが人間の手に残る。node01 に root で入り、**CI がビルドしたのと
同じ commit** を指して打つ:

```bash
nixos-rebuild boot --flake 'github:hikuohiku/homelab/<commit-sha>?dir=nix/images/proxmox-cloud#proxmox-cloud' && reboot
```

- `<commit-sha>` は **CI の `build` job が緑になった commit**。ブランチ名（`main`）でも動くが、
  sha を指すと「CI がビルドしたものと同じもの」を打っていることが確実になる。
- `#proxmox-cloud` は省略できない。`--flake` の `#name` を省くと**ホスト名**（`node01`）が
  使われるが、flake の出力は `nixosConfigurations.proxmox-cloud` である。
- `switch` ではなく `boot` を使う。nixpkgs 更新はカーネルを動かすことが多く、カーネルの
  切り替えにはどのみち再起動が要る。`boot` は「次回起動から有効」なので、
  稼働中の k3s を中途半端な状態にしない。
- **node01 は単一ノード。再起動中はクラスタ上の全アプリが止まる。**

打つ前に中身を見たいなら（任意）:

```bash
nixos-rebuild build --flake 'github:hikuohiku/homelab/<commit-sha>?dir=nix/images/proxmox-cloud#proxmox-cloud' --diff
```

（`--diff` は `/run/current-system` と新しい closure の差分を出す。
`build` / `boot` / `test` / `switch` で使える。）

## 5. ロールバック（(A) の場合）

```bash
nixos-rebuild list-generations          # 世代の一覧
nixos-rebuild boot --rollback && reboot # 1 つ前の世代へ戻して再起動
```

`--rollback` は `/nix/var/nix/profiles/system` の「現在」世代の 1 つ前へ戻す。
起動できないところまで壊した場合は、**GRUB のブートメニューから前の世代を選ぶ**
（世代は設定を変えるたびにメニューへ追加される）。

リポジトリ側のロールバックは `flake.lock` の revert 1 本。**revert しただけでは実機には何も起きない**
— node01 は古い世代のまま動いており、lock は「次にビルドしたら何になるか」を決めているだけ。

## 6. (B) image 差し替え — DR 用。今は実行できない

1. **リリース発行**: `release-image.yml`（`workflow_dispatch`）を**人間が**実行する。
   器の token では 403 で叩けない（`ops/CHARTER.md` §5.4）。qcow2 と sha256 が GitHub Release に付く。
2. `terraform/proxmox/variables.tf` の `nixos_image_version`（と `nixos_image_checksum`）を上げる。
3. `just apply`（= `terraform apply`）。

**3 は T-0107（pveproxy 証明書の SAN 不一致）で禁止されたまま。** さらに 2 を上げて apply すると
`replace_triggered_by` が効いて **VM が再作成され、`local-path` PVC は失われる**。
実行するなら restic からの復元が前提（`docs/backup.md`）。

## 7. 腐敗の検知

`ops/check_flake_freshness.py` が CI（`ci.yml` の `ops` job = 必須チェック）で走る。

- `flake.lock` の各 input の `lastModified` が **60 日**より古ければ**落ちる**。
- **45 日**を超えたら警告を出す（落とさない。閾値に触る前に気づくため）。
- `ops/inventory.json` の `nixpkgs.current` が lock の rev と一致しなければ落ちる。

落ちると全 PR がマージ不能になる（＝ autopilot のループが止まる）。そのため失敗メッセージ自体に
直し方（この文書の §1〜§3）を出すようにしてある。閾値の根拠はスクリプト内のコメントに書いた。

## 8. 実測と未確認

**実測（2026-08-10、P-0055）**

- 器の実行環境に `nix` は無い。`workflow_dispatch` は 403、`repository_dispatch` は 204。
  Actions の job ログは REST で平文のまま取れる。
- `terraform` の `replace_triggered_by` による VM 再作成（上記のとおりコードで確認）。
- `nixos-rebuild` の `--flake flake-uri[#name]`（name 省略時はホスト名）・`--rollback`・
  `boot` / `test` / `switch` の意味は、**lock している nixpkgs の rev そのもの**の man ソース
  （`pkgs/by-name/ni/nixos-rebuild-ng/nixos-rebuild.8.scd`）で確認した。
- ビルドの実測値（sha256・サイズ・所要時間・k3s / カーネルの before→after）は
  `ops/projects/logs/P-0055/image-build.md`。

**未確認（これを読む人が引き受けるリスク）**

- **§4 のコマンドを実機で通したことはまだ無い。** 器は node01 のホスト OS に届かないため。
  CI で `system.build.toplevel` が通ることは「評価とビルドが成立する」ところまでの裏付けであって、
  実機で `boot` → 再起動が成功する保証ではない。具体的に残る懸念:
  - この構成は `proxmox-image.nix`（image ビルド用モジュール）を含んだまま。image を作るための
    設定が、稼働中のシステムへの適用でどう振る舞うかは実機でしか分からない。
  - cloud-init が注入した設定（ホスト名・ネットワーク・SSH 鍵）と、再起動後の状態の整合。
  - sops の age 鍵（`/var/lib/sops-nix/key.txt`）と
    `sops-install-secrets-after-cloud-init` サービスが、新しい世代でも同じ順序で回るか。
  - node01 上でのビルドに必要なディスクと時間（substituter は cache.nixos.org と
    hikuohiku.cachix.org が `configuration.nix` に設定済み）。
- k3s のマイナー更新が apps/ の manifest に与える影響は、更新のたびに読み直すこと
  （前回 T-0062 では「影響なし」だったが、それは前回の版の話）。

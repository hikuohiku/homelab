# P-0055 — アプリだけ新しく、土台は古びる（NixOS / k3s の更新を器の定常業務にし、1 サイクル回す）

## 目的

inventory の 39 対象のうち 37 はコンテナと chart で、追従は P-0029 で再開した。しかしその全部が
乗っている土台 — nixpkgs（= カーネル・k3s・systemd）は `policy: manual` で、`last_checked` の欄すら
無い。「pin は誰も上げなければ据え置かれる」（CLAUDE.md、vaultwarden #49）はアプリ層で起きた事故で、
**同じ構造が土台層にそのまま残っている。** しかも土台の更新は image build → VM 差し替えという別経路で、
器は一度も通したことがない。この 1 サイクルを実際に通し、腐敗を CI で機械検知にする。

## 受入チェックリスト

initializer が実測した結果、**7 項目とも現時点で failing**
（2026-08-10、`project/p-0055` の checkout で、リポジトリルートから実行）。

- [ ] `python3 -c "import json,sys; d=json.load(open('nix/images/proxmox-cloud/flake.lock')); sys.exit(0 if d['nodes']['nixpkgs']['locked']['lastModified'] > 1785828668 else 1)"`
  — flake.lock の nixpkgs が実際に進んだこと。実測 rc=1（現在値がちょうど `1785828668` =
    2026-08-04T07:31:08Z、6.2 日前。**閾値ではなく現在値そのもの**なので、1 秒でも進めば通る）。
- [ ] `test -f ops/check_flake_freshness.py` — 腐敗検知スクリプトが存在すること。実測 rc=1。
- [ ] `python3 ops/check_flake_freshness.py` — **それが green であること**。実測 rc=2（ファイルが無い）。
- [ ] `grep -q 'check_flake_freshness' .github/workflows/ci.yml` — CI に実際に配線されていること。実測 rc=1。
- [ ] `test -f docs/os-updates.md` — 人間向けの一本道が文書として存在すること。実測 rc=1。
- [ ] `grep -q 'k3s' docs/os-updates.md` — その文書が k3s に言及していること（空ファイルで
    前項だけ通す抜け道の番人）。実測 rc=2（ファイルが無い）。
- [ ] `test -f ops/projects/logs/P-0055/image-build.md` — ビルドの実測記録が残ること。実測 rc=1。

**verify は DoD の下限であって DoD そのものではない。** 7 本のうち **DoD (2)「image を実際に
ビルドする」を見張っているものは 1 本も無い**（`image-build.md` の存在だけ。中身は誰も見ていない）。
ビルドを回さずに文章だけ書けば verify は全部 green になる — **そうしないこと。**
`image-build.md` には後述の実測値（run URL・sha256・バイト数・所要時間・k3s / カーネルの before→after）を
**ログからコピーした生の値**で貼る。要約や再構成をしない。

## 設計方針

### 前提（initializer が 2026-08-10 に実測した。調べ直さなくてよい）

- **この実行環境に `nix` は無い**（CHARTER §5.2、`command -v nix` で再確認済み）。したがって
  `nix flake update` は手元で打てない。**`flake.lock` を手で書くこともできない** —
  `narHash` は nix でしか計算できず、でっち上げれば `nix flake check` が NAR hash mismatch で落ちる。
  → **更新もビルドも GitHub Actions の中でやる。ここが本プロジェクトの一番の制約。**
- **`workflow_dispatch` は叩けない。実測 403**
  （`POST /repos/hikuohiku/homelab/actions/workflows/<release-image.yml の id>/dispatches`
  → `{"message":"Resource not accessible by personal access token","status":"403"}`）。
  → **`release-image.yml` は器から起動できない。リリース発行は人間専有のまま**（§5.4 の位置づけは変わらない）。
- **`repository_dispatch` は叩ける。実測 204**（`POST /repos/hikuohiku/homelab/dispatches`、
  リスナー無しのイベント型で確認）。ただし repository_dispatch は **default ブランチの workflow 定義しか
  実行しない**ので、**このプロジェクトが main に入るまでは使えない**（将来の定常運転用の入口として仕込む）。
- **`.github/workflows/**` への push はできる**（workflow scope は 2026-08-07 に付与済み。
  P-0027 が `watchdog.yml` を実際に追加できている＝実測済み）。
- **Actions の job ログは REST で平文のまま取れる。実測 200**
  （`GET /repos/hikuohiku/homelab/actions/jobs/<job_id>/logs`、`-L` でリダイレクト追従、16 KB のテキスト）。
  各行に `2026-08-10T13:05:16.377Z ` の時刻接頭辞が付く。
  → **artifact も PR コメントも要らない。ビルド結果はログから直接読める。** `unzip` もこの環境にある。
- `ci.yml` の `nix` job は **`nix flake check --no-build`（評価のみ）**。ビルドが重いので意図的にそうしてある
  （L183-187 のコメント）。**ここに `nix build` を足すと全 PR が数十分になる** — 別 workflow を新設し、
  paths フィルタで nix 配下の変更時だけ回す。
- **ruleset の必須チェック一覧は人間専有**（§5.2）。新しい job を足しても必須にはできない（P-0027）。
  → **`check_flake_freshness.py` は既存の `ops` job の 1 ステップとして足す**（新 job を作らない）。
    `ops` job は `ubuntu-latest` + 素の `python3` だけで回るので、**stdlib 以外に依存しない**こと
    （`check_version_sync.py` 冒頭のコメントと同じ方針）。
- **`configuration.nix` は `services.k3s` のバージョンを pin していない**（L133-138、`enable`/`role`/
  `extraFlags` のみ）。これが T-0049/#146 の見落としの本体で、nixpkgs を動かすと k3s が黙って動く。
  カーネルも `boot.kernelPackages` 未指定＝nixpkgs 既定に従う。
- inventory の `nixpkgs` エントリ（`ops/inventory.json`）の `note` に **「次のホップ（k3s 1.36 系）では
  containerd 2.x が必須（1.35 が containerd 1.x をサポートする最終版）」** と既に書いてある。
  CHARTER §4 の「note の除外条件を必ず読み合わせる」（T-0149）はこの行のことである。
- lock の現在値（2026-08-10T13:12Z 時点の age）: nixpkgs 6.2 日 / flake-parts 8.6 日 /
  nixpkgs-lib 15.5 日 / **sops-nix 37.0 日**。→ 60 日閾値なら **今はどれも green**。
  ただし sops-nix は放置すると 2026-09-02 ごろに閾値へ触る。
- **image を差し替えると node01 の VM が再作成される。** `terraform/proxmox/nixos-image.tf` の
  `proxmox_download_file.nixos_image` を `vm-nixos.tf` の
  `lifecycle { replace_triggered_by = [proxmox_download_file.nixos_image.id] }` が参照している。
  → `nixos_image_version` を上げて apply すると **VM ごと作り直しになり、`local-path` PVC は消える。**
    そのうえ T-0107（pveproxy 証明書の SAN 不一致）で apply は禁止されたまま。
    **つまり image 経路は「通常の OS 更新」ではなく DR / 再プロビジョニングの経路である。** ここを
    取り違えたまま docs を書かないこと。

### 決めてあること（この方針で作る。変えるなら理由を PROGRESS.md に書く）

1. **新しい workflow を 1 本足す**（例 `.github/workflows/nixos-image.yml`）。
   トリガは `on: push`（`branches-ignore: [main]`、paths: `nix/images/proxmox-cloud/**` と
   その workflow 自身）＋ **将来用に `on: repository_dispatch: types: [flake-update]`**。
   `workflow_dispatch` は 403 で叩けないので入口にしない。ジョブは 2 つ:
   - **`update`**: `nix flake update` を走らせ、(a) 更新前後の
     `nixosConfigurations.proxmox-cloud.config.services.k3s.package.version` と
     `config.boot.kernelPackages.kernel.version` を `nix eval` で出力し、(b) **新しい flake.lock を
     base64 1 行でログに出す**（開始・終了マーカー付き）。**リポジトリへ push しない。**
     — CI からブランチへ push させると、worker の未 push commit と非 fast-forward で衝突する。
       ログに出して worker が写す方が安全で、写した値がそのまま commit される。
   - **`build`**: `nix build ./nix/images/proxmox-cloud#packages.x86_64-linux.qcow2`
     （`release-image.yml` L70 と同じ経路）。sha256・バイト数・所要時間（`date +%s` の差）を出す。
     併せて **`config.system.build.toplevel` もビルドする** — これが通ることが、後述の
     `nixos-rebuild` 一本道が机上の空論でないことの機械的な裏付けになる。
2. **手順（この順に踏む。飛ばすと「更新したつもり」で終わる）**
   1. workflow を commit → wrapper が push → `update` job が回る。
      run / job の id は `GET /repos/hikuohiku/homelab/actions/runs?branch=project/p-0055` から取る。
   2. ログから base64 の lock を取り出して `nix/images/proxmox-cloud/flake.lock` に書き、commit。
      **`git diff` で 4 input（nixpkgs / nixpkgs-lib / flake-parts / sops-nix）の rev と
      lastModified が動いたことを目で確認する。**
   3. その push で `build` job が回る＝**commit した lock そのものがビルドされる**。
      ログから sha256 / サイズ / 所要時間 / k3s / カーネルを取り、`image-build.md` に貼る。
      **ビルドが通らないなら通るまでが仕事**（disk 不足なら不要ディレクトリの削除、
      評価エラーなら configuration.nix 側の追従）。落ちた回も run URL とエラーを記録する。
   4. `update` job が出した before→after を根拠に、**k3s とカーネルのリリースノートを原文で読む**
      （CHARTER §4「現在版から目標版まで全部」。この環境では curl が 403 になっても WebFetch が通ることがある）。
      読んだ結果を `image-build.md` に書く。「breaking なし」と書くなら**何を読んでそう言えるのか**を添える。
3. **k3s がマイナーを跨ぐなら、跨がせない。** `update` job の eval で k3s が **1.36 系以上**に動くと
   分かった場合は、同じ PR で `configuration.nix` に `services.k3s.package = pkgs.k3s_1_35;`
   （属性名は eval で実在を確認すること）を **pin として入れ、現状の挙動を保つ**。
   理由: inventory の note のとおり 1.36 は containerd 2.x を要求し、これは CHARTER §4 の
   「戻せる形に落とす」対象。**土台の更新を定常業務にするとは、k3s のホップを nixpkgs 更新の
   副作用ではなく明示の決定にすることでもある**（T-0049 の再発防止そのもの）。
   pin したら PROGRESS.md の「発見」節に「k3s 1.36 へのホップ」を別プロジェクト候補として残す。
   1.35 系のままなら pin は入れない（1 PR 1 論点）。
4. **`ops/check_flake_freshness.py`** — stdlib のみ。`nix/images/proxmox-cloud/flake.lock` の
   `nodes` を走査し、各 input の `locked.lastModified` が今より **60 日**より古ければ非 0 で終わる。
   - 閾値は**スクリプト内の定数**でよい（rules.json を触らない）。**根拠をコメントに書く**:
     nixos-unstable のチャンネル更新は数日〜十数日おきで、60 日はその何周期分にも当たる一方、
     カーネル / k3s の修正が四半期放置されるのは防げる長さ、という趣旨を自分の言葉で書く。
   - **失敗メッセージに直し方を書く**（何日古いか・どの input か・`docs/os-updates.md` を見よ・
     更新の起動方法）。これは `ops` job = **必須チェック**に入るので、落ちた瞬間に全 PR が
     マージ不能になる＝ループが止まる。**落ちたときに次の起動がそのまま直せる形で落とすこと。**
   - 45 日を超えたら（落とさずに）警告行を出す。閾値に触る前に気づけるようにする。
   - **これは CHARTER §4 の「縛る変更」**（これまで無かった失敗条件の新設）。PR 本文にその旨と
     ロールバック（このステップを消す revert 1 本で戻る）を明記する。
5. **inventory の nixpkgs エントリに欄を足す**（DoD 4）。`current` を実際の locked rev にし、
   `upstream_rev`（最後に見た上流の rev）と `last_checked`（UTC の ISO8601、CHARTER §7.2）を足す。
   **腐らせない工夫として、`check_flake_freshness.py` が `current` と flake.lock の rev の一致も
   検査する** — lock を更新して inventory を直し忘れたら CI が落ちる。
   `ops/validate.py` の `check_inventory` は id/kind/name/current/file/upstream/policy の非空と
   file の実在しか見ないので、追加フィールドは自由に足せる（既存エントリの形は壊さない）。
6. **`docs/os-updates.md`**（DoD 5）。一本道を、**実測と未確認を区別して**書く。
   - flake 更新 → image build: 上の workflow の使い方（**将来は `repository_dispatch` で起動できる**
     — event type と client_payload を書く。`workflow_dispatch` が 403 で使えない事実も残す）。
   - **VM 差し替えには 2 つの経路があり、既定は image ではない**ことを最初に書く:
     - **(A) in-place（既定）**: node01 上で `nixos-rebuild` を打つ。VM は作り直さない。
       PVC も消えない。ロールバックは前世代（`nixos-rebuild --rollback` / ブートメニュー）。
     - **(B) image 差し替え（DR・再プロビジョニング用）**: リリース発行（人間、`workflow_dispatch`）→
       `nixos_image_version` 更新 → `terraform apply`。**`replace_triggered_by` で VM が再作成され、
       `local-path` PVC は失われる。T-0107 が解けるまで apply 禁止。** 実行するなら restic からの
       復元が前提（`docs/backup.md` を参照）。
   - **人間に渡す手作業は「(A) のコマンド 1 回」に削る。** node01 の root で
     `nixos-rebuild boot --flake 'github:hikuohiku/homelab?dir=nix/images/proxmox-cloud#proxmox-cloud'`
     （＋再起動）に相当する 1 行へ落とし、**それ以外の判断・準備は全部こちらで済ませてから渡す。**
     コマンド文字列は思い込みで書かず、`nixos-rebuild --help` 相当の一次情報で裏を取ること。
   - **未確認は未確認と書く。** 器は node01 のホスト OS に届かない（Pod は node01 上だがホストではない）ので、
     (A) を実機で通したことはまだ無い。CI で `config.system.build.toplevel` が通ることは
     「評価とビルドが成立する」ところまでの裏付けであって、実機で switch が成功する保証ではない
     （`proxmox-image.nix` を含む構成をそのまま switch できるか、cloud-init / sops 鍵まわりに
     差分が出ないかは実機でしか分からない）。**残る懸念を具体的に列挙して人間に渡す。**
   - `ops/check_doc_commands.py` が **docs 中の `just` レシピの実在を検査する**（CI の ops job）。
     存在しない `just` レシピを書かないこと。

### ロールバック

- repo 側は全て追加で、revert 1 本で戻る（workflow / スクリプト / CI ステップ / docs / inventory の欄）。
- `flake.lock` も revert で前の rev に戻る。**戻すだけなら実機には何の影響もない** —
  node01 はまだ古い image で動いており、lock は「次にビルドしたら何になるか」を決めているだけ。
- 実機に触るのは (A) の 1 コマンドを人間が打った後だけで、そのロールバックは前世代へのブート。

## やらないこと

- **`terraform apply` と、それを誘発する変更**（`nixos_image_version` の更新を含む）。T-0107 で禁止。
  VM 再作成＝PVC 全損の経路であり、DoD も「差し替えの実行は人間の手に残る」と明記している。
- **リリースの発行**（`release-image.yml` の実行、タグ付け、GitHub Release 作成）。
  `workflow_dispatch` が 403 で物理的にできない。docs に手順を書くところまで。
- **アプリ層（`apps/`）の更新**。`touches_apps: false`。chart もコンテナタグも触らない（P-0029 の領分）。
- **k3s 1.36 系への実際のホップ**。containerd 2.x の確認が要る別論点。跨ぎそうなら pin して止め、
  PROGRESS.md の「発見」に残すだけにする。
- **`ci.yml` に新しい job を足すこと**。既存 `ops` job のステップとして足す（新 job は ruleset の
  必須チェックに入れられず、壊れていてもマージできてしまう — P-0027 / §5.2）。
  ビルド用の**別 workflow** を新設するのはこれとは別（必須チェックにする必要が無いため）。
- **`ops/rules.json` / `ops/backlog.json` / `ops/state.json` / `ops/journal/` / CHARTER / VISION /
  `ops/memory/` の更新**。heart が直接 main に push する領分で衝突する（CLAUDE.md、worker プロンプト）。
  `substrate.md` に昇格させたい実測（dispatch の 403/204、job ログの平文取得）が出ても、
  **ここでは書かず** PROGRESS.md に残して consolidation に渡す。
- **`ops/check_version_sync.py` への登録**。nixpkgs は二重管理 pin（`mirrors`）を持たない。
- **`nix flake check` の内容変更、既存 `nix` job の改変**。評価のみに留めてある設計を崩さない。

# P-0055 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

**実測の証拠はここに貼る。** verify 7 本のうち **DoD (2) の image ビルドを見張っているものは
`image-build.md` の存在チェック 1 本だけで、中身は誰も見ていない。** ビルドの run URL・sha256・
サイズ・所要時間・k3s / カーネルの before→after は `image-build.md` が本体、ここには結論と経緯を書く。
貼らなかった分は、存在しなかったことになる。

## セッションログ

<!-- ### s1 (YYYY-MM-DD) — 見出しに「何が終わったか」を書く -->

### s1 (2026-08-10) — CI 経路・腐敗検知・docs を入れた（verify 5/7 green。残り 2 本は実ビルド待ち）

**やったこと**（commit `f2a99df4`, `8df28a65`）

1. `.github/workflows/nixos-image.yml` を新設。`update` job（`nix flake update` + before/after の
   k3s / カーネル / NixOS 版を `nix eval` + 新 lock を base64 1 行で出力、**push はしない**）と
   `build` job（`system.build.toplevel` と `packages.x86_64-linux.qcow2` を実ビルド、
   sha256 / バイト数 / 所要時間 / closure サイズを出力）。
   入口は「main 以外への push（paths: `nix/images/proxmox-cloud/**` と workflow 自身）」＋
   `repository_dispatch: [flake-update]`。`workflow_dispatch` は器の token では 403 なので入口にしない。
2. `ops/check_flake_freshness.py`（stdlib のみ）＋ `ci.yml` の `ops` job に配線（DoD 3）。
   60 日で落ちる / 45 日で警告 / inventory の `nixpkgs.current` と lock の rev の一致も検査。
3. `ops/inventory.json` の nixpkgs に `upstream_rev` / `last_checked` を追加し、`current` を
   説明文（`"flake.lock の rev"` という文字列だった）から実 rev に変えた（DoD 4）。
4. `docs/os-updates.md`（DoD 5）＋ CLAUDE.md からの導線。
5. nixos-image.yml が checkout / nix-installer / cachix-action の pin を二重管理にするので、
   inventory の `mirrors` と `check_version_sync.py` に最初から登録した（T-0114 の穴を作らない）。

**実測（s1 時点）**

- 上流 `nixos-unstable` の head = `f13ff45afd1bb73e640eaa08a7066dbed07e3238`（committer date
  2026-08-07T12:45:23Z）。lock は `e72e4f29…`（2026-08-04）。**3 日分の差がある＝更新すれば
  verify 1 本目（lastModified > 1785828668）は通る。**
  取得: `curl -sS https://api.github.com/repos/NixOS/nixpkgs/commits/nixos-unstable`（無認証で 200）。
- lock の age（2026-08-10 実測）: nixpkgs 6.2d / flake-parts 8.6d / nixpkgs-lib 15.5d / sops-nix 37.0d。
  60 日閾値では全部 green。**sops-nix が最初に閾値へ触る（2026-09-02 ごろ）。**
- `check_flake_freshness.py` は失敗側も手で確認した（閾値を 20/10 に落として sops-nix がエラー・
  nixpkgs-lib が警告になること、rev をずらすと inventory 不一致で落ちること、
  lastModified を持つ input が 0 件なら「空振り」として落ちること）。
- `AUTOPILOT_GITHUB_TOKEN` で Actions API は読める（`GET /repos/hikuohiku/homelab/actions/runs`
  → 200, total_count 1315）。job ログもこの token で取れるはず（PROJECT.md の実測どおり）。
- 手元 verify: 5/7 green。**残り 2 本は `flake.lock` の更新と `image-build.md`** で、どちらも
  「CI を 1 往復させる」ことが前提。

**次のセッションへ**（この順に踏む。s1 の push で `update` / `build` 両方が既に回っているはず）

1. run を拾う:
   ```bash
   curl -sS -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN" \
     "https://api.github.com/repos/hikuohiku/homelab/actions/runs?branch=project/p-0055&per_page=20"
   # → 目当ての run id → /actions/runs/<id>/jobs → /actions/jobs/<job_id>/logs （-L でリダイレクト追従）
   ```
2. `update` job のログの `P0055_FLAKE_LOCK_BASE64_BEGIN` / `END` に挟まれた 1 行を
   `base64 -d > nix/images/proxmox-cloud/flake.lock`。**ログ行には `2026-08-10T13:05:16.377Z ` の
   時刻接頭辞が付くので、剥がしてから decode すること。** `sha256sum` もログに出しているので突き合わせる。
3. **同じ commit で `ops/inventory.json` の nixpkgs の `current` / `upstream_rev` / `last_checked` を
   必ず更新する。** 忘れると `check_flake_freshness.py` が落ちて全 PR がマージ不能になる
   （＝自分で仕掛けた罠に自分でかかる）。commit 前に `python3 ops/check_flake_freshness.py` を回す。
4. その push で `build` job が **新しい lock そのもの**をビルドする。ログから
   run URL / sha256 / バイト数 / 所要時間 / k3s・カーネルの before→after を
   `ops/projects/logs/P-0055/image-build.md` に**生の値のまま**貼る（要約しない）。
   **落ちた回も run URL とエラーを残す。** ビルドが通らないなら通るまでが仕事。
5. `update` job の `P0055 after services.k3s.package.version` を見て、**k3s が 1.36 系に跨ぐなら
   pin して止める**（`services.k3s.package = pkgs.k3s_1_35;` — 属性名は eval で実在を確認）。
   1.35 系のままなら pin は入れない（1 PR 1 論点）。跨ぐ場合は「発見」節に別プロジェクト候補として残す。
6. before→after が確定したら k3s とカーネルのリリースノートを原文で読み、`image-build.md` に書く。
   「breaking なし」と書くなら**何を読んでそう言えるのか**を添える。

**未解決の罠 / 途中の仮説**

- `nix eval` の属性 `config.services.k3s.package.version` が実在するかは**未確認**。
  job は失敗しても止まらないようにしてあり、その場合ログには version ではなくエラー文が出る。
  出ていたら `…package.name`（`k3s-1.35.6` のような形）に切り替える。
- qcow2 のビルドが GitHub ホストランナーのディスクに収まるかは**未確認**。先回りして
  dotnet / android / ghc / boost / hostedtoolcache を消す step を入れ、前後で `df -h /` を出している。
  それでも足りなければ `/mnt`（65 GB 程度ある）へ nix store を逃がす手が残っている。
- `concurrency` は意図的に付けていない。cancel-in-progress は走行中のビルドを消し、
  cancel なしの直列化は次のビルドを待たせる。どちらもビルド 1 回の実測を失う。
- YAML の block scalar（`run: |`）の中で**列 0 に文字を置くとブロックが終わる**。
  workflow に python を埋めるときは heredoc の終端も含めて必ずインデントを保つこと
  （s1 で 1 度踏んだ）。`python3 -c` の複数行も同じ理由で使えない。
- `image-build.md` は**まだ作っていない**。存在チェックだけで verify が緑になる本なので、
  実データが入るまで空ファイルを置かないこと（PROJECT.md の警告どおり）。

## 発見（仕様外。ここに書くだけにして、後で curriculum が拾う）

- **nix installer が 2 系統に割れている。** `ci.yml` の `nix` job は `cachix/install-nix-action@v31`、
  `release-image.yml`（と s1 で足した `nixos-image.yml`）は `DeterminateSystems/nix-installer-action@v22`。
  s1 は「release-image.yml と同じビルド経路」を優先して後者に揃えたが、リポジトリ全体としては
  どちらか 1 つでよいはず。統一すれば監視対象も 1 つ減る。
- **`last_checked` を持つ inventory 対象が nixpkgs だけになった。** 他の 38 対象は「最後に上流を
  見た日」を持たないので、追従が止まっていても台帳からは分からない（止まっていた事実は
  #49 で事故になった）。tag 追従の対象にも同じ欄を入れると、腐敗の距離が全対象で見える。
- **image のリリース発行だけが人間専有として残った。** `workflow_dispatch` が器の token では
  403 で、`release-image.yml` は起動できない。ビルド自体は s1 の workflow で回せるようになったので、
  残るのは「タグを打って Release を作る」部分だけ。ここを `repository_dispatch` 経由に寄せられれば
  (B) 経路も器の手に入る（ただし `terraform apply` は T-0107 で禁止のままなので、
  入れても差し替えの実行には届かない）。

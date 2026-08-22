# P-0105 — repo と B2 から全部戻した日、暗号だけが開かない — SOPS age 鍵の依存地図を作り、復元 dry-run で「開かない日」を先に演じる

## 目的

nix/images/proxmox-cloud/secrets.yaml (SOPS/age) は node01 cloud-init シークレットの唯一の
暗号化源だが、age 秘密鍵の所在・復元経路は repo のどこにも文書化されていない。node01 全損時
(P-0054 が演じようとして止まった筋) に鍵が無ければ暗号化資産は全滅する。鍵の依存地図を機械化し、
復号 dry-run で「開かない日」を先に演じておく (P-0063 restic 演習とは対象と層が違う。
本番データには触れず、復号 dry-run に限定する)。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-22、`project/p-0105` の checkout で、リポジトリルートから実行)。

- [ ] `python3 ops/tools/sops_dependency_map.py > /dev/null && test -f ops/sops-dependency-map.json`
  — 依存地図スクリプトが存在し、実行すると JSON (ops/sops-dependency-map.json) を産出すること。
  実測 rc=2 (`ops/tools/` ディレクトリ自体が存在しない)。
- [ ] `python3 -m unittest ops.tests.test_sops_dependency_map`
  — 地図スクリプトの固定テスト。実測 FAILED (errors=1、モジュール不在による import error)。
- [ ] `grep -q '## 復元手順' docs/sops-recovery.md`
  — 復元手順ドキュメントが存在し、「## 復元手順」節を持つこと。
  実測 rc=2 (`docs/sops-recovery.md` が存在しない)。

**verify は DoD の下限であって DoD そのものではない。** 3 本とも「ファイルがあるか / 文字列が
あるか」しか見ず、(a) JSON の中身が実在の依存と一致するか、(b) 手順書が**実際に手順として使えるか**
(鍵の所在確認 → 復元 → 復号まで誰かがなぞれるか)、(c) dry-run の証跡は verify が一切見張っていない。
spec の dod どおり、dry-run の成否はどちらも価値だが、その帰趨と証跡は `PROGRESS.md` に残すしかない。

## 設計方針

### 前提 (initializer が 2026-08-22 に実測・実読した。調べ直さなくてよい)

- repo 内の SOPS 暗号化ファイルは現状 **1 つ**: `nix/images/proxmox-cloud/secrets.yaml`
  (中身は `doppler-token` 1 キー)。`.sops.yaml` の creation_rules もこのパス 1 本のみ。
  他ディレクトリに ENC[ マーカーを持つ YAML は無いことを grep で確認済み。
- 鍵→暗号→消費者の連鎖は実読で確定済み:
  `terraform/proxmox/variables.tf` の age 秘密鍵変数 ("Age private key for sops-nix decryption")
  → `terraform/proxmox/vm-nixos.tf` の cloud-init snippet が `/var/lib/sops-nix/key.txt` へ注入
  → `nix/images/proxmox-cloud/configuration.nix` の `sops.age.keyFile` がそれを読み
  `./secrets.yaml` を複号 → 同 `k3s-manifests.nix` が `sops.templates` で doppler-token Secret を生成。
  **CI (.github/workflows/) からの参照はゼロ。**
- つまり鍵の単一源は「Terraform 実行時に外から渡す変数」であり、repo には値がない。
  鍵の実体が確実に残っている場所は node01 上 1 箇所のみ = 全損時の単一障害点そのもの。
  credential map (P-0077/P-0071) はクラスタ credential 対象でこの鍵は載っていない。
- エージェント環境の実測: sops バイナリ無し (substrate.md 記載どおり)、`SOPS_AGE_KEY` 系 env 無し、
  `~/.config/sops/age/` も無い。→ spec の分岐では「入手経路なし」の incident 報告に倒れる可能性が
  高いが、**判定は worker が改めてその時点の環境で実測して下すこと** (環境は変わる)。
- 形は既存流儀に寄せる: スクリプトは stdlib + PyYAML のみ (CI ランナーにあるもの)、テストは
  `ops/tests/test_sops_dependency_map.py` として unittest discover 対象化
  (`test_check_credential_map.py` 同型)。verify #2 が `ops.tests.` を名指すのはこのため。

### 決めてあること

- 地図スクリプトは `.sops.yaml` の creation_rules と本文の `ENC[` マーカーの両方から暗号化ファイルを
  探索的に発見する (「secrets.yaml」というハードコードにしない — 2 つ目の暗号化ファイルが生えたとき
  勝手に守られる形にする)。JSON へは少なくとも「ファイル・recipient・消費者 (nix build /
  cloud-init / 手順書)・鍵の所在」を出力する。
- 復号 dry-run は 1 回だけ・出力は捨てる (stdout/stdin をファイルへ残さない。平文を証跡に貼らない)。
- `docs/sops-recovery.md` は docs/ 既存の手順書 (`backup.md`, `node01-storage.md`) の流儀で書く。
  「鍵の所在確認方法」と「## 復元手順」節を必ず持たせる (verify #3 が後者を見る)。

## やらないこと

- **鍵のローテーション・再発行、`.sops.yaml` / `secrets.yaml` の内容変更**。地図を作るだけで地形は変えない
- **B2/restic 側の復旧演習**。P-0063 系の論点 (1 PR 1 論点)
- **CI 配線の追加**。spec の verify に CI 項目が無い。地図の定期検査への組み込みは別論点
  (ci.yml の必須チェック追加は人間専有という制約もある)
- **本番データへのアクセス・node01 への SSH 実施・Terraform の実行**。静的な依存の整理と
  ローカルでの復号 dry-run に留める
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)

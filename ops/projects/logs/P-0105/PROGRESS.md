# P-0105 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-22) — initializer (PROJECT.md 作成)

- 受入 verify 3 本を実測し、**全項目 failing を確認** (rc=2 / unittest errors=1 / rc=2)
- 鍵→暗号→消費者の連鎖を実読で確定: `variables.tf` (age 秘密鍵変数) → `vm-nixos.tf`
  cloud-init (`/var/lib/sops-nix/key.txt`) → `configuration.nix` (`sops.age.keyFile`)
  → `secrets.yaml` 復号 → `k3s-manifests.nix` (`sops.templates`)。CI からの参照はゼロ。
  詳細は PROJECT.md「前提」
- エージェント環境に鍵の気配なし (env / `~/.config/sops` 共に無し、sops バイナリも無し)。
  dry-run の帰趨 (復号成功 or incident 報告) は worker がその時点の環境で改めて実測して判定すること

### セッション 2 (2026-08-22) — worker (受入 #1 #2 を green 化)

**やったこと**

- `ops/tools/sops_dependency_map.py` を新規作成。`.sops.yaml` の creation_rules と
  本文の実物形 `ENC[...]` + sops メタデータの両方から暗号化ファイルを探索的に発見し、
  鍵名 / recipient / 消費者 (nix・terraform・ci / doc・log は記録のみ) / 鍵の所在
  (Terraform 変数・node01 の key.txt・この環境の在否) を `ops/sops-dependency-map.json`
  に出力する。ハードコード無し。生成物はタイムスタンプを持たず diff が汚れない
  (同一走査でバイト等しいことを実測)
- `ops/tests/test_sops_dependency_map.py` を新規作成 (26 tests)。合成 fixture で
  fail-closed の各筋を両方向固定 + 実リポジトリテストで地形 (recipient・消費者・
  鍵連鎖・CI 参照ゼロ) を固定。discover 全体 (94 tests) も OK
- **verify #1 rc=0 / verify #2 OK を自分で実測済み**。verify #3 (`docs/sops-recovery.md`)
  は未着手 — これが残る唯一の failing

**分かったこと (発見)**

- エージェント環境を実測し直した (2026-08-22 セッション 2 時点): sops / age バイナリ無し、
  `SOPS_AGE_KEY` / `SOPS_AGE_KEY_FILE` 無し、`~/.config/sops/age/keys.txt` 無し。
  地図 JSON の `agent_environment.can_decrypt_now: false` が証跡。
  **環境が変わらない限り dry-run は「incident 報告」に倒れる**
- ruff / pip はこのサンドボックスに無い → F821 検査は CI でのみ効く。ローカルでは
  目視確認しかできない
- unittest discover の stdout には heartbeat 系テスト由来の `post:` / `heartbeat:` 行が
  既存で混ざる。異常ではない (`Ran 94 tests ... OK` が本体)

**次のセッションへの一言**

1. `docs/sops-recovery.md` を書いて verify #3 を green にする。流儀は `docs/node01-storage.md`
   同型。「鍵の所在確認」(terraform var `age_private_key` は repo 外の値 / node01 の
   `/var/lib/sops-nix/key.txt` が唯一の実体 = 全損時の単一障害点) と「## 復元手順」節
   (鍵の確保 → `SOPS_CONFIG` 位置に注意して `sops -d` → 平文はファイルへ残さない) を持たせる
2. docs の後、その時点の環境でもう一度 probe (`python3 ops/tools/sops_dependency_map.py`
   の `can_decrypt_now` か手動の env/binary 確認) を取り、**「入手経路がエージェントに
   存在しない」incident 報告**をこの PROGRESS の発見として確定させる (spec dod の分岐。
   復号できるなら代わりに 1 回だけ復号して rc のみ記録)
3. 罠: `ENC[` は PROJECT.md 等の散文にも出てくる。検出緩和 (マーカー単独判定への変更) は
   誤検出を生むのでしないこと。一時ファイルは `/tmp/opencode` でなく必ず `mktemp`
   (この環境では /tmp/opencode が書き込めない)

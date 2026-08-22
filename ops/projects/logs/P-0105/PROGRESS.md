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

### セッション 3 (2026-08-22) — worker (受入 #3 を green 化、dry-run の帰趨を確定)

**やったこと**

- `docs/sops-recovery.md` を新規作成 (`docs/node01-storage.md` 同型の流儀)。
  「鍵の所在確認」節 (Doppler homelab/prd の `AGE_PRIVATE_KEY` が一次情報源 / node01 の
  `/var/lib/sops-nix/key.txt` が実体コピー = 全損時単一障害点 / repo に値は無く recipient のみ /
  `age-keygen -y` での対合検証) と「## 復元手順」節 (repo ルート実行・`SOPS_CONFIG` の注意・
  平文をファイルへ残さない dry-run 手順)、加えて「鍵を失った場合」節 (被弾範囲は doppler-token
  1 キーのみ、再発行可能なので環境ごと作り直せる) を持たせた
- 鍵供給経路の裏付けを実読で確定: justfile の plan/apply は `doppler run --project homelab
  --config prd --name-transformer tf-var` 経由で Terraform 変数に値を渡す。apps/README.md にも
  「Age キーペアを生成して Doppler に保存 (`AGE_PRIVATE_KEY`)」とある。→ 復元手順は Doppler を
  第一候補に書いた
- `ops/sops-dependency-map.json` を再生成 (新 doc への言及が doc 種別で載るのみ。problems 空)。
  **verify #1 #2 #3 すべて自分で rc=0 / OK を実測済み**
- spec dod の分岐を確定: その時点の環境で probe を取り直したところ sops/age バイナリ無し、
  `SOPS_AGE_KEY*` env 無し、`~/.config/sops/age/keys.txt` 無し → **「鍵の入手経路がエージェントに
  存在しない」incident 報告として記録する** (下記発見)。復号実施の分岐は不発

**分かったこと (発見)**

- 【incident】エージェント環境には age 秘密鍵の入手経路が存在しない (2026-08-22 セッション 3 実測。
  証跡は `ops/sops-dependency-map.json` の `agent_environment.can_decrypt_now: false`)。
  「開かない日」の演習としては、node01 全損 + エージェント単独の復旧は不可能という結論が先に
  出たことになる。鍵の一次情報源 (Doppler) への人間の到達手段が復元計画の要 — docs に書いた通り
- docs に暗号化ファイル判定を誤爆しない注意: 本物の形の `ENC[AES256_GCM,data:<長いbase64>` と
  行頭から始まる YAML マッピングのメタデータブロックを**両方**含む md を書くと、走査がそれを
  暗号化ファイルと誤検出する (is_sops_encrypted は AND 判定)。sops-recovery.md はどちらも
  含めずに書いた。将来この文書系を編集する人は同じ罠に注意
- 地図 JSON はコミット済み生成物であり、docs/log の言及変化で差分が出る (今回も PROGRESS.md と
  新 doc 分が乗った)。これは仕様通り。コミット直前に必ず再生成して同期させる

**次のセッションへの一言**

1. 受入 3 本はすべて green (セッション 3 commit 時点)。残るは wrapper の verify 再実測とレビュー。
   差し戻しがあればその解消が最優先
2. レビューで「復号を実際にやれ」と出たら: その時点の環境で probe を取り直すこと。鍵が現れて
   いる場合だけ 1 回だけ復号し rc のみ記録 (平文は残さない)。無ければ incident 報告のままで正しい
3. 罠の引継ぎ: `ENC[`+メタデータの同時混入で doc が誤検出される話 (上記) と、JSON 再生成忘れに
   注意。一時ファイルは `mktemp` (/tmp/opencode 不可は変わらず)

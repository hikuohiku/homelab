# P-9065 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。
文脈は PROJECT.md とこのファイルと git log のみ。

## セッション 1 (2026-08-25) — 全 deliverable を実装し verify 4 項目を green にした

### やったこと

1. **`ops/tools/secret_recoverability.py` 新規** — ExternalSecret 全件を apps/ から静的走査し、
   Doppler キーごとに `recoverable` (allowlist 内) / `doppler_only` (allowlist 外) を分類して
   `ops/health/secret-recoverability.json` へ出力。`--selftest` 付き、fail-closed
   (ExternalSecret 0 件・dataFrom・recovery_path 未定義・rules.json 欠落は rc=1)。
   先例 (sops_dependency_map.py / check_credential_map.py) の流儀を踏襲。
2. **`ops/tests/test_secret_recoverability.py` 新規** — 18 テスト。allowlist 内外の分類、
   dataFrom の分類不能検出、全キー recovery_path 保持、欠損時の fail-closed、
   実リポジトリの地形 (26 keys / allowlist 10 / unclassifiable 0) を固定。
3. **`ops/health/secret-recoverability.json` 生成物** — schema_version 1、keys 26
   (recoverable 10 / doppler_only 16)、unclassifiable []、problems []。再実行で
   diff が出ないことを確認 (決定的)。
4. **`docs/recovery-plan.md` 新規** — 分類規則の説明 + 全 26 キーの recovery path を列挙。
   秘密値は含めない (verify 4 の assert を自走で確認)。

### 入力元の決定 (PROJECT.md が worker に委ねた点)

**apps/ の ExternalSecret manifest の静的走査を採用** (クラスタ到達不能でも再現可能)。
本チェックアウトの apps/ が 26 キーを列挙でき、check_credential_map.py の
DECLARED_DOPPLER_KEYS (26) と一致することを実測した。spec の「25 件超」はクラスタ実態
(P-0175 時点 21 item) だが、manifest ベースの再現可能な入力で 26 キー ≧ 25 を満たす。

### 分類規則

rules.json の `allowed_autopilot_doppler_keys` (現実は **10 件**。PROJECT.md 初期化時点の
「3 件」はその後 allowlist が拡張されたため古い) に載る → recoverable、載らない →
doppler_only。allowlist は事実として読み、結果に合わせて曲げていない。

### verify 全 4 項目の自走実測 (全て rc=0)

```
$ test -f ops/tools/secret_recoverability.py                                   # rc=0
$ python3 -m pytest ops/tests/test_secret_recoverability.py -q                 # 18 passed
$ test -f ops/health/secret-recoverability.json && python3 -c "import json;d=json.load(open('ops/health/secret-recoverability.json'));assert d['keys'] and all(k.get('recovery_path') for k in d['keys'])"   # rc=0
$ test -f docs/recovery-plan.md && ! grep -qE 'BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY' docs/recovery-plan.md   # rc=0
```

追加の確認: `python3 -m unittest discover -s ops/tests -t .` = 609 tests OK、
`python3 ops/check_credential_map.py` rc=0、`python3 ops/validate.py` rc=0、
`python3 ops/check_doc_commands.py` rc=0。ruff F821 はこの環境に pip が無く実行不可
(代わりに AST で未定義名を走査し 0 件)。CI 側で確認される。

### 分かったこと / 罠

- **PROJECT.md の「allowlist 3 件」は古い** (現在 10 件)。分類結果 (recoverable 10) は
  ツールが rules.json を生で読むので実態に追従する。文書には「2026-08-25 時点の実測」として
  10 件を記した。
- **ops-state 移行の話は主 apps/ にはほぼ影響していなかった**: 本チェックアウトの apps/ に
  ExternalSecret は全て残っており (adguard/autopilot-core/nats/telegram-adapter/version-watcher
  も含む)、26 キーが得られた。ops-state の apps/ は subset (vaultwarden/immich/coder/dex/… のみ)。
- **GITHUB_HEALTH_REPORTER_TOKEN の実参照は version-watcher のみ** (ops-health-reporter は
  現状 ExternalSecret を持たず、token を再利用)。check_credential_map の宣言と実体は一致。
- `secretKey` (k8s 側の名前) と `remoteRef.key` (Doppler 側) は別物。分類は remoteRef.key だけを
  読む。ops-dashboard は NATS_DASHBOARD_NKEY_SEED を secretKey=NATS_NKEY_SEED で参照している。

### 次のセッションへの一言

- **verify は全項目 green。受入検証を持つ仕様なので、完成の宣言は wrapper の実測待ち。**
  この PR で追加したのは新規 4 ファイル + PROGRESS 追記のみで、既存動作は変更していない。
- 残論点 (仕様外、curriculum が拾う候補): (a) ops-health-reporter の
  GITHUB_HEALTH_REPORTER_TOKEN が実参照を持たなくなった件、(b) 本ツールの CI 常設
  (check_credential_map 同様 consistency checks へ) は verify が要求していないので未実施。
- NATS NKey の recoverable は「器で作り直せる」であって「同じ鍵が戻る」ではない。
  docs/recovery-plan.md に注意書きを入れてある。レビューで突っ込まれたらそこを参照。

## 発見 (スコープ外。後で curriculum が拾う)

- PROJECT.md の「allowlist 3 件」が実際は 10 件に拡張済み (分類の文脈では影響なし)。
- ops-health-reporter が GITHUB_HEALTH_REPORTER_TOKEN の実参照を持たない (version-watcher が
  再利用。manifest 上は check_credential_map と一致している)。
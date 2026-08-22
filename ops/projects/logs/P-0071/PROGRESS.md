# P-0071 — 進捗

各セッションはここの末尾と git log しか読まない。何をやったか / 分かったこと /
次のセッションへの一言を、セッションごとに追記する。

## セッションログ

### セッション 1 (2026-08-22) — 実装完了、verify 2 項目 green

**やったこと**

- `ops/check_credential_map.py` を新規作成。3 種の突き合わせを実装:
  (1) ExternalSecret `remoteRef.key` ↔ `DECLARED_DOPPLER_KEYS` (20 種)、
  (2) ExternalSecret `target.name` ↔ `DECLARED_SECRET_TARGETS` (18 種)、
  (3) workload の `secretKeyRef`/`envFrom.secretRef` 参照先が apps/ 内の manifest
  (ES target または静的 Secret) と同じ namespace で作られていること。
  加えて腐り防止の逆方向検査 (宣言だけ残って参照が消えたら落ちる) と
  EXEMPT_SECRET_CONSUMERS 免除表 (理由必須・現状空)
- `ops/tests/test_check_credential_map.py` を作成 (合成 fixture で両方向 + 実 repo 検査)。
  CI 既存の unittest discover ステップから自動で走る。`--selftest` でも単独実行可
- ci.yml の "consistency checks" に 1 行配線 (新 job は作っていない)

**実測 (自分で回した証拠)**

```
$ python3 ops/check_credential_map.py
ok: apps/ の credential 参照は宣言と一致しています (Doppler keys 20 種 / k8s Secrets 18 種 / secretKeyRef 参照 15 組)
rc=0

$ test -f ops/check_credential_map.py && grep -q check_credential_map .github/workflows/ci.yml  → 両方 OK
$ python3 -m unittest discover -s ops/tests -t . → Ran 77 tests, OK (うち本件 18)
$ python3 ops/validate.py → OK
```

効能の e2e 証明 (PROJECT.md の要求「合成 fixture を確実に落とす」):
apps/zz-e2e-check/ に未宣言キーの ExternalSecret を置くと rc=1 で
`::error::Doppler キー 'BRAND_NEW_UNDECLARED_KEY' …` が出た。作り手の無い Secret を
secretKeyRef する Deployment でも rc=1 (`CreateContainerConfigError になる` の旨)。
削除後 rc=0 に戻ることを確認済み。

**分かったこと / 発見**

- initializer の PROJECT.md 記載「secretKeyRef は (secret,key) 組 8 種」は**過小**
  。CronJob の pod spec は `spec.jobTemplate.spec.template.spec` と一段深く、素朴な
  走査は CronJob 分を黙って取りこぼす。正しく辿ると消費ペアは **15 組** (41 usage)。
  テスト `test_scan_actually_sees_something` が restic CronJob 由来のランドマークで
  この罠を見張る
- 全 secretKeyRef 参照先は既に repo 内 ES target が作っている → 初期免除は空でよい。
  「実態 = 宣言」からの出発を実現できた
- ES の `spec.dataFrom` (extract/find) は静的に列挙できないので fail-closed で落とす
  実装にした (現状 repo に使用例なし)。将来出たら checker 拡張か data 形へ誘導
- 死角は docstring に明記済み: helm values 由来の参照 (immich/dex values.yaml)、
  manifest 文字列埋め込みスクリプト内の参照 (coder workspace-home-backup)、
  secretStoreRef のプロバイダ差し替え

**次のセッションへの一言**

実装は verify 込みで完了しているはず。レビューで差し戻されたらその指摘のみ直す。
スコープ外として残してある話: rules.json の `allowed_autopilot_doppler_keys` との
交差検査 (rules.json 側の腐り検知) は意図的にやっていない — PROJECT.md の
「rules.json は触らない」の一歩手前なので、やりたくなったら curriculum に上げること。


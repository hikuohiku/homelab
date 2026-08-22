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

### セッション 2 (2026-08-22) — 自己レビューで fail-open を見つけて修正

レビュー verdict が空だったので、自分でコードレビュー (セッション 1 の実装を読み直し)
した。2 件の潜在バグを実測で確定 → 修正 → リグレッションテスト追加。すべて green。

**やったこと**

1. **fail-open 修正 (重大)**: `main()` が「problems あり・violations 0 件」のとき
   `::error::` を印字しつつ rc=0 を返していた。dataFrom や壊れた YAML など
   fail-closed 対象が CI を緑で通る — docstring が掲げる契約そのものを破っていた。
   合成 fixture で「dataFrom ES + 宣言整合の ES」を回し rc=0 を実測 → `problems or
   violations` で落とすよう修正 → 同 fixture で rc=1 を再測
2. **`_pod_spec` の未知 kind 契約違反修正**: `_POD_SPEC_PATHS` に無い kind
   (ConfigMap 等) で空パス→空ループ→doc 自身を返しており、「workload 系で無ければ
   None」に反していた。top-level `containers` を持つ非 workload 文書が誤走査されうる。
   path is None なら即 None を返すよう修正
3. リグレッションテスト 3 本追加 (`test_pod_spec_returns_none_for_unknown_kind`,
   `TestMainFailClosed.test_problems_without_violations_still_fails`,
   `.test_clean_fixture_passes`)。main() テストは APPS_DIR / DECLARED_* /
   sys.argv を mock patch して実 repo を汚さずに exit code 契約を固定する

**実測**

```
$ python3 -m unittest discover -s ops/tests -t . → Ran 80 tests, OK (+3)
$ python3 ops/check_credential_map.py
ok: ... (Doppler keys 20 種 / k8s Secrets 18 種 / secretKeyRef 参照 15 組) rc=0
$ test -f ops/check_credential_map.py && grep -q check_credential_map .github/workflows/ci.yml → 両方 OK
$ python3 ops/validate.py → 0 error, 2 warning (heartbeat STALE / comments.json 読めない。
  どちらも heart 領域・この sandbox checkout の環境ノイズで P-0071 とは無関係と判断)
```

**分かったこと / 発見**

- 「verify 全項目 green」「実 repo で rc=0」は fail-open を全く検出しない。問題は
  「異常を検知したときの出口」側にだけ起きるので、正常系しか通っていない限り永遠に
  見えない。教訓: 検査器を作ったら、**異常経路の exit code を合成 fixture で固定する**
  のが DoD 相当 (セッション 1 は violations 側の合成テストはあったが problems 側の
  main() 出口テストが抜けていた)
- 実 repo の走査結果 (20/18/15) は修正前後で不変。修正は異常経路のみに影響

**次のセッションへの一言**

機能面の実装と自己レビューは完了。レビュー指摘が出たらそれだけ直すこと。
なお validate.py の heartbeat STALE warning は本プロジェクト外 (heart の領域)。


### セッション 3 (2026-08-22) — `.yml` 拡張子の fail-open を潰す

レビュー verdict は再び空だったので、3 回目の精査。走査対象の**拡張子**に穴を
見つけて修正した。

**やったこと**

1. **`.yml` 拡張子の取りこぼし修正**: `scan_apps` が `rglob("*.yaml")` のみを見ており、
   `apps/**/*.yml` に置かれた manifest は黙って列挙から漏れる。合成 fixture で
   「整合する .yaml + 未宣言キーの .yml」を回すと problems=0 / violations=0 で
   **素通しを実測** (fail-open)。`*.yml` も走査するよう修正 → 同 fixture で
   違反 2 件 (未宣言キー + 未宣言 Secret 名) を落とすことを再実測
2. リグレッションテスト `test_yml_extension_is_also_scanned` 追加
   (.yml の参照が doppler_keys / secret_targets に入ることを固定)

**実測**

```
$ python3 -m unittest discover -s ops/tests -t . → Ran 81 tests, OK (+1)
$ python3 ops/check_credential_map.py --selftest → rc=0
$ python3 ops/check_credential_map.py
ok: ... (Doppler keys 20 種 / k8s Secrets 18 種 / secretKeyRef 参照 15 組) rc=0
$ test -f ops/check_credential_map.py && grep -q check_credential_map .github/workflows/ci.yml → 両方 OK
$ python3 ops/validate.py → 0 error, 2 warning (セッション 2 と同じ heart 領域の環境ノイズ)
```

**分かったこと / 発見**

- fail-open は「ロジックの出口」だけではなく「入力の網羅」にも起きる。セッション 2 は
  異常経路の出口を潰したが、今回は「走査が特定のファイル名パターンを見ていない」という
  網羅側だった。教訓: 検査器の**入力集合の定義そのもの** (どのファイルを見るか) も、
  見逃しを実証する合成 fixture で固定する
- 実 repo には現時点で .yml/.json manifest が 1 つも無い (`find apps -name "*.yml"` 空)。
  なので実走査結果 (20/18/15) は修正前後で不変。将来 .yml を足した人が勝手に守られる
- 精査は他にも回した: `_docs` の read_text 失敗 (OSError 等) は main() の catch-all で
  rc=1 (fail-closed)、charts 除外・namespace スコープ一致・免除表の逆方向検査は問題なし

**次のセッションへの一言**

機能面の実装と 3 回の自己レビュー完了。レビュー指摘が出たらそれだけ直すこと。
これ以上の自己レビューは収穫逓減と思われる (出口・入力網羅・純関数両方向は固定済み)。

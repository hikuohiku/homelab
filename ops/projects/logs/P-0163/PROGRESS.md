# P-0163 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。

## セッション 1 — 2026-08-23: 検査ツール本体 + unittest (verify 1・2 を green 化)

### やったこと

- `ops/tools/syncthing_acceptance.py` 新設 (stdlib のみ)。`check` サブコマンドが
  検査リストを `{name, required, status, detail}` で集約して表示し exit code を返す。
  必須 7 項目: identity-files / device-id-format / self-device-declared /
  folder-paths / pvc-rw / restic-coverage / (restic マニフェスト読めないときのみ unknown)。
  任意 2 項目: gui-health / tailnet-sync。**FAIL は必須/任意を問わず rc=1、
  UNKNOWN は単独では許容だが --strict で rc=1** (「応答が異常」のような確定的な
  否定情報を見逃さないため。到達できないことだけは落とさない — version_watch 流儀)
- device ID 導出は syncthing 本家 lib/protocol/deviceid.go + luhn.go の移植。
  sha256(DER) → base32 (52 文字, pad除去) → 4 個の luhn32 検査数字挿入 (56 文字)
  → 7 文字区切り (8 グループ)。**ゴールデンベクトルは本家のテストから移植**
  (`P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2`、旧 52 文字形式・
  タイポ修正 0→O/1→I/8→B も受ける)。正規化は canonical_device_id() に集約
- 空回し演習 (DoD 2) は `exercise` サブコマンド。ダミーフォルダ acceptance-dummy の
  登録 → 書き込み → rescan 収束待ち → 読み戻し → restic 静的カバレッジ確認 →
  **後始末は finally で必ず実行** (フォルダ削除 + ディレクトリ削除)。
  exercise の exit code は UNKNOWN も不合格 (演習は確定させるものなので)
- `ops/tests/test_syncthing_acceptance.py` 新設 (37 テスト、ネットワーク注入で無通信)。
  実マニフェストに対する restic-coverage テストは cronjob の意図しない変更の drift 検知も兼ねる

### verify 自己実測

```
$ test -f ops/tools/syncthing_acceptance.py            # rc=0
$ python3 -m unittest ops.tests.test_syncthing_acceptance   # Ran 37 tests ... OK
```

CI 相当も通過確認: `python3 -m unittest discover -s ops/tests -t .` → Ran 279 tests OK。

### 分かったこと / 実測

1. **この runner 環境からクラスタ Service に直接届く** (2026-08-23 実測):
   `getent hosts syncthing.syncthing.svc` → 10.43.228.154、
   `GET http://syncthing.syncthing.svc:8384/rest/noauth/health` → 200 `{"status": "OK"}`、
   TCP 22000 to syncthing-sync ClusterIP (10.43.6.132) 接続成功。
   つまり wrapper 実行でも gui-health/tailnet-sync は「不明」でなく green に出る。
   --strict は restic マニフェストさえ渡せばこの環境から成立する
2. **cert.pem/config.xml の配置には 2 通りの可能性があり未確定**: repo 内で
   restic cronjob の exclude は `config/index-v2` 表記なのに backup コメントは
   裸の `config.xml` を列挙している。ツールは flat (PVC 直下) / nested (config/ 配下)
   を自動判別するが、docs に「置き場所」を書くときは実物で確定すること
   (PROJECT.md 前提の「PVC root 直下・mountPath 実測」なら flat)。ここを推測で書かないこと
3. fixture 証明書は手組み DER ではなく公開ルート CA (Amazon Root CA 3) を採用。
   最初に ASN.1 手組みで作ったら OpenSSL パーサーに何度拒否され時間を溶かした
   (乱数ストリーム 128 バイト制限で s が切れていた等)。runner 環境に openssl/
   cryptography は無いが、システム CA バンドルから真正な自己署名証明書を
   取り込む方が早くて確実。期待値 (b52/canonical) は ssl+hashlib+base64 の
   独立経路で算出し、luhn 部だけ本家ベクトルで担保する構成にしたので
   「自分の実装の焼き直し」テストになっていない
4. exercise CLI は稼働中 syncthing の設定を変える (専用フォルダの追加/削除のみとはいえ)。
   動作試験は必ず死んだポート宛てで (`--gui-url http://127.0.0.1:9`)。

### 次のセッションへの一言

残りは verify 3 の `docs/syncthing-migration.md`。含めるべきは:
LXC 101 での tar 1 コマンド (`tar -C /var/lib/syncthing -czf - .` とパス要確認注記)、
取り出したファイルの置き場所 (上記 2 の確定待ち。所有権 1000:1000 への言及)、
検証コマンド 1 回 (check --strict + exercise の起動方法。in-cluster 一時 Job か
port-forward かを決めてコマンド形に落とす)、失敗時ロールバック
(**LXC 101 は合格まで停止しない** / 追加した config 差分を抜いて新規インストールに戻す)。
罠: `/tmp/opencode` はこの環境では書けない (Permission denied、`/tmp/st-cert` を使用)。
ruff バイナリも無い (py_compile で代用済み)。

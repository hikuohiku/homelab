# P-0304 — AdGuard が自分の設定を書き戻して堕ちる夜を、立ち上がりの門で止める — YAML 構文検証ゲートと last-known-good ローテーション (PVC 手術はしない)

## 目的

現に進行中の障害 (latest.json 2026-08-24T07:30Z: adguard CrashLoopBackOff restarts=90、
yaml construct error line 13) の再発防止。git 側の種設定は正常で、壊れているのは AdGuard Home
自身が UI 変更を書き戻す PVC 上の稼働設定 — つまり「アプリ自身が書くファイルの破損で、
次回起動から永久ループに入る」構造が AdGuard に内在しており、修繕 (採択済み P-0285) を
何度繰り返しても同じ死に方をし得る。P-0285 が今日の応急処置なら、こちらは予防:
起動毎に設定の構文を検証し、壊れていたら最後の正常コピー (last-known-good) から自動復旧して
鳴らす。PVC の外科手術は P-0285 の領域なので触らず、manifest 側 (initContainer ゲート +
バックアップローテーション) だけを変えることで競合しない。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-24、`project/p-0304` の checkout のリポジトリルートで実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -m pytest ops/tests/test_adguard_config_guard.py -q`
  — 破損 YAML fixture で「破損 → LKG 復旧」「LKG 無し → seed + incident」の分岐を
    自己証明する unit test が存在し green であること。
    実測 rc=1 (`No module named pytest`)。**テストファイル未存在に加え、この checkout 環境には
    pytest も pip も無い** (`python3 -m ensurepip` は可能)。CI は unittest で回す
    (.github/workflows/ci.yml:58 `python3 -m unittest discover -s ops/tests -t .`)。
    詳細は設計方針 6。
- [ ] `bash ops/adguard/config_guard.sh --selftest`
  — ガードスクリプトがクラスタ外でも fixture を使った自己診断モードで自己証明できること。
    実測 rc=127 (`ops/adguard/config_guard.sh: No such file or directory`。ディレクトリごと未存在)。
- [ ] `grep -q 'config-guard' apps/adguard/deployment.yaml`
  — Deployment に検証ゲートの initContainer が配線されていること。
    実測 rc=1 (deployment.yaml の initContainer は `seed-config` 1 本のみ)。
- [ ] `test -f docs/adguard-config-guard.md && grep -q 'last-known-good' docs/adguard-config-guard.md`
  — 設計と「PVC 設定が正、seed は種」の注意を書いた文書が存在すること。
    実測 rc=1 (docs/adguard-config-guard.md 未存在。docs/ には adguard 関連なし)。

## 設計方針

前提は initializer が 2026-08-24 に実読・実測した。調べ直さなくてよい。

1. **現状の起動経路**: `apps/adguard/deployment.yaml` の initContainer は `seed-config`
   1 本で、「PVC に設定が無い初回だけ種を植える」(`test -f … || cp …`)。
   そして `ops/tests/test_adguard_manifest.py` の
   `test_image_is_pinned_identically_in_both_places` が **initContainers は 1 個・名前は
   seed-config・image は本体と同値** を契約として固定している。config-guard を足すなら
   この契約を意味を保ったまま更新する (何をどう固定し直したかをテストのコメントで書くこと)。
   既存契約「conf は書き込み可能」「seed は初回だけ・直接 mount しない」は維持する。
2. **YAML 検証は正規のパーサーで行う**: 今回落ちたのは `yaml construct error` であり、
   grep/regex の近似判定では同じ死に方を通してしまう。AdGuard 公式イメージ内の
   toolchain (python3/yaml の有無) は未検証。一方、autopilot イメージ
   `ghcr.io/hikuohiku/homelab-autopilot` は python3 (+py3-yaml) 同梱を実測済み
   (ops/memory/substrate.md、verified_at 2026-08-06) で node01 が既に pull 済み。
   guard 用 initContainer のベースイメージ選定はこの事実を起点に worker が決める
   (公式イメージだけで完結するならそれが望ましい。確認せず「無い」と決めつけない)。
3. **上書き方向は常に「PVC → バックアップ」**: 正常判定された設定をタイムスタンプ付きで
   LKG ディレクトリへローテーション保存する (配置例: PVC の conf サブパス配下、保持世代は
   明示値を決めてコメントに書く)。破損時は最新の正常コピーへ復旧してから本体を起動する。
   seed が PVC に書くのは「正常コピーが一つも無い」場合のみ — 既存 seed-config の
   「初回だけ」と整合する位置づけにする (seed-config との実行順も含めて worker が整理)。
   LKG は conf 配下にあるので既存の restic backup CronJob (conf を readOnly mount) の
   保護対象に自然に乗る。backup 側は一切変えない。
4. **incident 通知 (分岐 c)**: 既存の push 経路は `DISCORD_WEBHOOK_URL`
   (ops/heart/notify.py、ops/rules.json の allowlist) だが adguard namespace には無い。
   ExternalSecret 型で webhook を渡す (apps/argocd で P-0139 が同型を計画中) か、
   initContainer ログ + marker ファイルで観測層 (health reporter / 人間) に委ねるかは
   worker 判断。ただし DoD (1)(c) の「incident 通知する」を空約束にしないこと —
   採った方式を docs に書き、通知が一度は実際に飛ぶ/残ることを示す証拠を PROGRESS.md に残す。
5. **テスト**: 既存パターンは `ops/tests/test_adguard_manifest.py` 流儀 — unittest.TestCase、
   ネットワークに出ない、fixture は inline か ops/tests/fixtures/ 配下。
   `ops/tests/test_adguard_config_guard.py` は subprocess で実物のスクリプトを実行し、
   fixture (正常 YAML / line 13 型破損の実物模写 / LKG 空ディレクトリ) で 3 分岐
   (a)(b)(c) を検証する。`--selftest` は同一 fixture を使う自己診断モードとする。
   CI が unittest discover なので **unittest 形式で書き、pytest でも green になるようにする**
   (unittest.TestCase は pytest がそのまま走らせる)。
6. **verify #1 の pytest 依存について**: この環境に pytest/pip は無いが ensurepip は使える。
   worker は着手早期に pytest を用意できるか確認し、受入時に `python3 -m pytest …` が
   実走できない事情が残る場合は PROGRESS.md に記録して報告すること
   (unittest への黙認すり替えは受入 verify の実測原則に反する)。
7. **docs/adguard-config-guard.md** に設計 (3 分岐・ローテーション・上書き方向) と
   「PVC 設定が正、seed は種」の注意を書く。docs/ の既存文体 (docs/syncthing-migration.md 等)
   に揃える。

### ロールバック

revert PR 1 本で戻る。manifest 差分は initContainer 追加 (+必要なら script 用 ConfigMap) のみで、
データ移行・削除を伴わない (LKG ディレクトリが PVC に残るだけ。Prune=false の PVC)。

## やらないこと

- **PVC の外科手術・現状診断・CrashLoopBackOff からの手動復旧** — P-0285 の領域。
  本プロジェクトは merge 順で自然に直列する。conflict したら rework 経路に乗る。
- **seed ConfigMap (apps/adguard/configmap.yaml) の内容変更** — http address / filters /
  upstream 等、種の中身は今のままで正。壊れていないものを直さない。
- **backup / restic 周りの変更** — schedule・鍵・retention は既存契約 (test_adguard_manifest.py
  TestResticCronJobs) のまま。LKG が保護対象に入るのは配置の帰結であって、backup 側は触らない。
- **UI 書き戻し自体の禁止** (conf の読み取り専用化等) — UI からの設定保存が壊れるので、
  「アプリが設定を書く」既存の構造は維持し、書かれた結果の破損を門で弾くのが本プロジェクト。
- **他アプリ (immich/vaultwarden 等) への横展開** — 同型の門が必要になってから別案で。
  1 PR 1 論点。
- **`ops/rules.json` / `ops/backlog.json` / `ops/state.json` / CHARTER・VISION・`ops/memory/`
  の更新**。heart が直接 main に push する領域と不可侵層。

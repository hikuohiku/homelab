# P-0163 — 母艦に残った最後のデータの引っ越し台本を完成させる — syncthing 移行の受け入れ検証を器が持ち、人間の残作業を 1 コマンドまで削る

## 目的

クラスタで動く実サービスの中で唯一、実データが旧 LXC 101 に置き去りのままなのが syncthing。
移行は cert/config の取り出し (人間にしか到達できない LXC のファイルシステム) を数週間待って止まっている
(seeds H5、旧 T-0140)。本案は「移行をする」ではなく**「移行の受け入れ検証を機械化し、人が打つコマンドを
1 つに削り込む」**に焦点を絞り直したもの (同型の P-0113 は不採択だった)。cert 取り出し後すぐ検証まで通る
状態を作れば宿題は「いつでも払える」になり、VISION 段階 2 の成果定義に直結する homelab 本体の仕事。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0163` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/tools/syncthing_acceptance.py`
  — 受け入れ検査ツール本体 (stdlib のみ) が存在すること。
  実測 rc=1 (`ops/tools/` に未存在)。
- [ ] `python3 -m unittest ops.tests.test_syncthing_acceptance`
  — 検査ロジックが unittest で固定されていること (fixture: 合成 config 断片)。
  実測 rc=1 (ModuleNotFoundError — モジュール未存在、FAILED)。
- [ ] `test -f docs/syncthing-migration.md && grep -q 'LXC 101' docs/syncthing-migration.md`
  — 人間の手順書が存在し、移行元 LXC 101 に言及していること。
  実測 rc=1 (`docs/syncthing-migration.md` 未存在。docs/ には backup.md 等はある)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) 検査がチェックリスト形式で合格/不合格を **exit code** で返すこと、(2) 合成データでの空回し演習が
自動化されていること、(3) 手順書に「取り出したファイルの置き場所」と「失敗したときのロールバック」が
明記されていること — は worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- 移行先の器は既にある: PVC `syncthing-data` (20Gi, 空, `Prune=false`)、Deployment
  `apps/syncthing/deployment.yaml` (`syncthing/syncthing:2.1.3`、PVC を `/var/syncthing` に mount、
  PUID/PGID 1000、Recreate)、GUI Service `syncthing.syncthing.svc:8384`
  (probe が `/rest/noauth/health` — GUI 認証設定に依らず疎通可)、tailnet sync 用 Service
  `syncthing-sync` (`apps/syncthing/service-tailnet.yaml`、LoadBalancer + loadBalancerClass: tailscale、
  22000 TCP/UDP + 21027 UDP)
- restic backup も既存: `apps/syncthing/restic-backup-cronjob.yaml` (b2:...:syncthing、backup + retention。
  docs/backup.md P-0047 で復元試験まで完了)。除外は `config/index-v2/` と `config/syncthing.lock`。
  「restic backup 対象か」の判定はこのマニフェストの対象 path / exclude を読む静的検査で足りる
- 人間の手順の元ネタは backlog T-0140 (needs-human): LXC 101 の `/var/lib/syncthing` (パス要確認) から
  cert.pem/key.pem/config.xml/index 一式を取得する。LXC への到達を持つのは人間だけ。
  配置先の正は **PVC root = `/var/syncthing` 直下** (mountPath 実測) で、所有権は 1000:1000
- autopilot Pod からは PVC 中身を読めない (exec/cp 禁止、CHARTER §5)。よって PVC 由来の入力を要する
  検査は「syncthing-data を mount した Pod/Job 内で実行する」設計にし、投入は Git → ArgoCD 経由か
  構築セッションの一時 Job に委ねる (spec `touches_apps: false` なので apps/ 配下に常設オブジェクトを
  増やさない)。どうしても同名 Job を再実行するなら Force=true,Replace=true (substrate.md 実測)
- unittest の既存パターン: `ops/tests/test_*.py` + `python3 -m unittest`、合成入力は
  fixture 文字列/`ops/tests/fixtures/` (test_download_ledger_script.py 等が先例)

### 作り方

1. `ops/tools/syncthing_acceptance.py` — stdlib のみ。検査を個別関数に分け、結果を
   `{name, ok, detail}` のリストで集約して表示し、必須検査が 1 つでも落ちたら rc!=0。
   入力が得られない検査 (tailnet 到達不能等) は沈黙せず「不明」として明示する (version_watch.py 流儀)
2. config 由来の確認: cert.pem (PEM→DER→sha256→base32) から device ID を導出して形式検査、
   config.xml の folder 定義の所在と path (/var/syncthing 配下に収まるか) を確認。
   導出・抽出ロジックは純関数に切り出し、合成 config 断片の fixture で unittest
3. 空回し演習 (DoD 2): ダミーの同期フォルダ 1 個を登録し、書き込み → rescan → 読み戻し →
   restic 対象確認までを自動化する。本番データには触れず、専用の dummy フォルダのみ触る
4. `docs/syncthing-migration.md`: 人間の手順を「LXC 101 で tar を 1 コマンド」+「検証コマンド 1 回」に
   整理。tar の置き場所 (PVC root、所有権 1000:1000 への言及含む) と失敗時のロールバック
   (**旧 LXC 101 は検証合格まで停止しない** / 失敗時は追加した config 差分を抜いて新規インストールに
   戻す) を明記する

## やらないこと

- **移行の実施そのもの (T-0140 本体)**。cert/config の取り出しと PVC への配置は人間 (場合により構築セッション) の
  鍵作業であり、needs-human のまま。本案が作るのは受け入れ検証の器と台本まで
- **旧 LXC 101 の停止・破棄**。検証合格後の後始末で別論点 (Plans.md の M1 vaultwarden と同じ順序)
- **本番データへの接触**。空回し演習は合成データのみ。restic 復元試験の再実施もしない (P-0047 済)
- **apps/ 配下の常設マニフェスト変更** (spec `touches_apps: false`)。syncthing 本体の機能変更・バージョン更新もしない
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする (CLAUDE.md)

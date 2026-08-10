# P-0054 — 「node01 が今日消えた」を演じきる

## 目的

backup は 5 対象すべてに配線された (P-0028/P-0047) が、**現行の構成のまま実際に戻して時間を計った
記録は無い**。さらに `docs/backup.md` が扱うのは PVC のデータだけで、k3s の CA / tailscale の
device identity / Doppler が持つ全 credential / coder の動的 workspace PVC といった
**「データではない資産」は復旧計画に一行も無い**。ノード全損は homelab で最も高くつく単一事象なのに、
その日に何分かかるのかを誰も知らない。ここでその数字を出し、戻らないものは戻らないと名指しする。

## 受入チェックリスト

initializer が実測した結果、**7 項目とも現時点で failing**
(2026-08-10、`project/p-0054` の checkout で、リポジトリルートから実行)。

- [ ] `test -f docs/disaster-recovery.md`
  — 復旧手順書が存在すること。実測 rc=1 (`docs/` は `backup.md` / `immich-postgres-upgrade.md` /
    `node01-storage.md` / `terraform-plan-in-ci.md` の 4 本のみ)。
- [ ] `grep -qE 'RTO|所要時間' docs/disaster-recovery.md`
  — **実測した時間が書かれていること**。実測 rc=2 (ファイルが無く grep 自体が失敗)。
- [ ] `grep -qE '復元できない|再発行が要る' docs/disaster-recovery.md`
  — **戻らないものを戻らないと書いていること**。実測 rc=2 (同上)。
- [ ] `test -f ops/tests/test_restore_coverage.py`
  — 「戻す側」を守る機械検査が存在すること。実測 rc=1
    (`ops/tests/` は `__init__.py` / `test_backup_coverage.py` / `test_check_heartbeat_fresh.py` /
    `test_dashboard_projects.py` の 4 本)。
- [ ] `python3 -m unittest ops.tests.test_restore_coverage -v`
  — そのテストが green であること。実測 rc=1 (`ModuleNotFoundError`)。
- [ ] `test -f ops/projects/logs/P-0054/restore-drill.md`
  — 復元の実出力が残ること。実測 rc=1。
- [ ] `grep -q 'sha256' ops/projects/logs/P-0054/restore-drill.md`
  — 原本との突き合わせをした証拠が残ること。実測 rc=2 (同上)。

**verify は DoD の下限であって DoD そのものではない。** 7 本のうち 5 本は「ファイル/文字列があるか」
しか見ない。とくに **DoD (1) の「5 件すべてを実際に restore して時間を計る」と DoD (3) の
「データでない資産の 3 分類」は verify が一切見張っていない**ので、`restore-drill.md` に
**コマンドと実出力をそのまま貼ること**が唯一の証拠になる。sha256 の 1 個も verify #7 は
「文字列 sha256 があるか」しか見ないことを忘れないこと。

## 設計方針

### 前提 (initializer が 2026-08-10 に実測・実読した。調べ直さなくてよい)

#### spec の `why` に事実の誤りが 1 つある — 訂正して進めること

`why` は「実際に戻したのは syncthing 1 件だけ」と書いているが、`docs/backup.md` の「復元試験」節には
**5 件すべての復元試験が記録されている** (immich 2026-08-05 / vaultwarden 2026-08-06 /
coder-postgres 2026-08-06 / coder workspace home 2026-08-07 / syncthing 2026-08-10)。
それでもこのプロジェクトが空振りにならない理由は 3 つあり、**PROGRESS.md と restore-drill.md の
冒頭にこの訂正を書くこと** (誤った前提のまま「新規性がある」と言わない):

1. **原本と sha256 で突き合わせたのは syncthing だけ。** 残り 4 件は `restore_rc=0` +
   ファイル数 + マジックバイト止まりで、「中身が原本と同一か」は誰も確認していない。
   DoD (1) が要求しているのはこちら。
2. **4 件の試験は append-only 鍵への切り替え (P-0028, 2026-08-10) より前**。当時は削除権限つき鍵で
   復元した。**現行の backup 用鍵 (`readFiles` のみ) だけで戻せるか**は syncthing でしか実測していない。
3. 記録された所要時間 (immich 16s / vaultwarden 9s / coder-postgres 8s / workspace home 31s) は
   3〜5 日前のスナップショットに対するもので、**「今日の最新スナップショットから」の数字ではない**。

#### 対象は「5 つの restic リポジトリ / 6 個の確認物」

DoD (1) は 6 個の名前を挙げて「5 件」と呼んでいる。実体は **restic リポジトリが 5 本**で、
immich リポジトリの中にライブラリ本体と日次 DB ダンプの 2 つが入っている、が正しい対応。

| # | backup CronJob (実測した実在名) | schedule (JST) | リポジトリ | 直近の実測規模 (docs/backup.md, 2026-08-10) |
|---|---|---|---|---|
| 1 | `immich-restic-backup` | 2:45 | `b2:$BUCKET:immich` | 82 files / 340.715 MiB。**中に日次 DB ダンプ `backups/*.sql.gz` が入る** |
| 2 | `coder-restic-backup` | 3:10 | `b2:$BUCKET:coder-postgres` | 1 file / 933.200 KiB (`pg_dump -Fc` 単一ファイル) |
| 3 | `coder-workspace-home-backup` | 3:30 | `b2:$BUCKET:coder-workspace-homes` | **オーケストレータ方式**。host `general` 32420 files / 2.973 GiB、host `test` 3156 files / 925.316 MiB |
| 4 | `vaultwarden-restic-backup` | 3:40 | `b2:$BUCKET:vaultwarden` | 4 files / 1.748 MiB |
| 5 | `syncthing-restic-backup` | 3:55 | `b2:$BUCKET:syncthing` | 6 files / 14.577 KiB |

retention CronJob も同名で 5 本ある (`*-retention`、日曜 3:45/4:00/4:10/4:30/4:50 JST)。
**retention は今回一切触らない**し、テストの対象にもしない (`forget --prune` は「戻す側」ではない)。

- **夜間の帯 2:45〜3:55 JST (17:45〜18:55 UTC) と重なる時間帯に手動 Job を起こさないこと。**
  `concurrencyPolicy: Forbid` は手動 Job には効かない。
- 使い捨て PVC に要る容量は合計でも 5 GiB 未満。node01 の root disk は 256 GiB なので余裕はある
  (`CLAUDE.md`)。ただし `coder-workspace-homes` の host `general` (2.973 GiB) だけは桁が違うので
  最後に回す。

#### 復元の実務 (P-0047 が確立した型。読まずに踏み直さないこと)

- **復元 Job には `CHOWN` / `FOWNER` / `DAC_OVERRIDE` の 3 capability が要る。**
  backup 側の `DAC_READ_SEARCH` だけでは足りない。restic は所有権を戻すので `CHOWN`、chown した
  **後**は root でも所有者ではないので `utimensat` に `FOWNER`。どれが欠けても
  **中身は全部書けるのに restic は `Fatal` で終わる**。この知識は T-0071 → T-0117 → P-0047 と
  **3 回独立に踏まれている**。4 回目をやらないこと。
- **restore の前に `rm -rf <target>/*` を必ず入れる** (再実行時、前回 chown 済みディレクトリが
  EACCES の原因になる)。
- **復元は append-only 鍵 (`<app>-restic-backup-credentials`) だけで完結する** (P-0047 実測)。
  削除鍵 (`<app>-restic-credentials`) を持ち出さない。
- **過去の復元 Job manifest が git 履歴に残っている。写して使える** (削除済み。`apps/` に戻さない):
  - `git show 229735e5^:apps/immich/restic-restore-verify-job.yaml`
  - `git show 213c6892^:apps/vaultwarden/restic-restore-verify-job.yaml`
  - `git show f196bd44^:apps/coder/restic-restore-verify-job.yaml`
  - `git show 54368178^:apps/coder/workspace-home-restic-restore-verify-job.yaml`
  - syncthing 版は `ops/projects/logs/P-0047/restore-drill.md` に実出力ごと載っている
- **一時物は `kubectl apply` / `kubectl create job` で入れて、終わったら `kubectl delete` する**
  (P-0047 が確立)。ArgoCD の tracking ラベルが付かないので prune の対象にならず、既存アプリの
  同期に影響しない。`just preview` は root `apps` の auto-sync を止める副作用があるので採らない。
- **`autopilot-writer` は secrets に一切の権限が無い** (`apps/autopilot/rbac.yaml`)。
  Pod spec から `secretKeyRef` で参照するのは通る (P-0047 実測)。`kubectl get secret` は通らない。
  PVC / Job / Pod / ConfigMap / ExternalSecret は全 namespace で `*`。
- **pods/log は読める** (`autopilot-writer` に `pods/log` あり)。過去の Job が
  `/dev/termination-log` にサマリを書いていたのは旧 `autopilot-reader` に log 権限が無かった
  時代の名残で、**今は `kubectl logs` でそのまま読める**。

#### テストと CI

- CI の ops job は **ubuntu-latest + `python3` だけ**。kustomize も helm も kubectl も無い。
  PyYAML は使える (`test_backup_coverage.py` が既に依存して通っている)。
- 既存 discover `python3 -m unittest discover -s ops/tests -t .` が `ops/tests/` を自動で拾う。
  **`.github/` は触らない** (新 job は ruleset の必須チェック追加が人間専有で merge 待ちになる、P-0027)。
- **`ops/check_doc_commands.py` が `docs/*.md` を走査する。** 新しい `docs/disaster-recovery.md` に
  `` `just <recipe>` `` を書くなら、justfile に実在するものだけにすること
  (実在: `plan` / `apply` / `destroy` / `ts-up` / `preflight` / `preview` / `preview-reset` /
  `preview-status` / `prepare`)。

#### データでない資産の下調べ (DoD 3 の出発点。ここから実測で埋める)

| 資産 | 今わかっていること | 実測の当て方 (案) |
|---|---|---|
| k3s の cluster CA / node token | `nix/images/proxmox-cloud/configuration.nix` の `services.k3s` は `--token` を**宣言していない**。初回起動時に生成され `/var/lib/rancher/k3s/server/` に置かれる = **Git にも B2 にも無い** | Pod 内の `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` の `notBefore` を読めば「初回ブート時に生成された」ことが実測で言える。host FS への到達は試して、駄目なら「届かない」と書く |
| sops age key (`/var/lib/sops-nix/key.txt`) | これが無いと `secrets.yaml` が復号できず、`doppler-token` が k3s に入らず、**ExternalSecret が 1 本も同期しない** = 復旧の最上流 | repo からは鍵の所在しか分からない。**人間の手元にしか無いなら「再発行が要る」ではなく「失うと戻らない」側** |
| Doppler の全 credential | repo 走査で **20 キー**を確認: `ADMIN_TOKEN` `AUTOPILOT_GITHUB_TOKEN` `B2_ACCOUNT_ID(_APPEND_ONLY)` `B2_ACCOUNT_KEY(_APPEND_ONLY)` `CLAUDE_CODE_OAUTH_TOKEN` `CODER_DB_PASSWORD` `CODER_DB_URL` `DEX_ARGOCD_CLIENT_SECRET` `DISCORD_WEBHOOK_URL` `GITHUB_HEALTH_REPORTER_TOKEN` `GOOGLE_OAUTH_CLIENT_ID/SECRET` `IMMICH_DB_PASSWORD` `RESTIC_B2_BUCKET` `RESTIC_PASSWORD` `TAILSCALE_CLIENT_ID/SECRET` `VAULTWARDEN_ADMIN_TOKEN` | `apps/*/[a-z-]*external-secret*.yaml` (13 ファイル) の `remoteRef.key` を機械的に列挙し、`kubectl get externalsecret -A` の実在と突き合わせる。**Doppler 自身のバックアップが無いことを名指しする** |
| **`RESTIC_PASSWORD`** | 上の 1 キーだが**別格**。これを失うと **B2 に全部あっても 1 バイトも復号できない** | 復旧手順の最上流に置く。「失うと戻らない」の筆頭 |
| tailscale の device identity / ACL | operator は `TAILSCALE_CLIENT_ID/SECRET` (OAuth) から device を再登録できる。**ACL は Tailscale 管理コンソール側にあり Git に無い** | `kubectl get pods -n tailscale` 等で到達できる範囲を実測。secrets は読めないので identity 本体は見えない。**見えないなら見えないと書く** |
| ArgoCD の admin ローカルアカウント | `apps/argocd/values.yaml` に有効化制御が無く、UI/CLI で変えたパスワードは Git にも Doppler にも無い (`docs/backup.md` T-0065) | 実害の大小 (Dex OIDC が主経路) まで含めて分類する |
| coder の動的 workspace PVC | `apps/coder/templates/personal/main.tf` が `coder-<workspace-id>-home` を作る。**PVC 自体は backup 対象だが、それを作る Coder の Terraform state は coder-postgres の中** | `kubectl get pvc -n coder -l app.kubernetes.io/name=coder-pvc` で実測列挙。「PVC は戻るが、それを再びマウントする workspace を作れるかは DB 次第」という依存の向きを書く |
| syncthing の device identity | `config/cert.pem` / `key.pem` = device ID そのもの。**backup に入っている** (P-0047)。再発行すると既存ピアから別デバイスに見える | 「戻る」側の例として書く。3 分類は「戻らないもの」だけの表ではない |

### 決めてあること (この方針で作る。変えるなら理由を PROGRESS.md に書く)

1. **`docs/disaster-recovery.md` は新規の 1 ファイル。`docs/backup.md` は書き換えない。**
   役割を分ける — `backup.md` は「どう取るか」の設計記録、`disaster-recovery.md` は
   「**上から順に実行すれば戻る**」手順書。同じ事実を 2 箇所に書かない (CHARTER §1)。
   相互リンクを張り、`backup.md` 側には**復元試験の記録が新しくなったことを指す 1 行だけ**足してよい。

2. **`ops/tests/test_restore_coverage.py` の検査契約**を先に固定する。`test_backup_coverage.py` と
   同じ構造 (ファイル走査 + 純関数 + 合成入力で両方向) にすること。

   - **単位は「backup CronJob の `metadata.name`」**。`apps/**/*.yaml` (`/charts/` 除外) から
     `kind: CronJob` かつファイル本文に `restic` を含むものを集め、**名前に `retention` を含むものを
     除く**。今日の期待値は上表の 5 本。手で維持する対応表を持たない (それが P-0047 で潰した穴と同型)。
   - **要求すること**: `docs/disaster-recovery.md` を `^## ` 見出しで節に割り、各 CronJob 名について
     「**その名前を本文に含み、かつ実測時間の行を持つ節**」が 1 つ以上あること。
   - **実測時間の行はこの書式に固定する**: `実測所要時間: <数値> 秒` (分でもよい)。
     正規表現でしか守れないので、**`未測定` / `TBD` / `N/A` が同じ行に出たら落とす**。
   - **canary**: 走査が壊れて空を返すと本体テストが黙って通る。5 本を実際に見つけていることを
     別テストで固定する (`test_backup_coverage.py` の `test_scan_actually_sees_something` と同じ形)。
   - **既知の死角を docstring に書く**: これは「docs に節と数字があるか」しか見ない。
     **数字が本当に実測かは機械には分からない** — 証拠は `restore-drill.md` にしかない。
     伏せずに書くことが次の棚卸しへの引き継ぎになる。

3. **実測の順序 (DoD 1)。小さい順にやる。** 1 件終わるごとに `restore-drill.md` に追記して
   **commit する** — セッションが落ちても測り直しにならないようにするため。
   1. `syncthing` (14.577 KiB) → 2. `vaultwarden` (1.748 MiB) → 3. `coder-postgres` (933 KiB、
      PVC ではなく単一ダンプ。`pg_restore --list` + `pg_restore -f /dev/null` で読めることまで) →
      4. `immich` (340.715 MiB。**ライブラリ本体と `backups/*.sql.gz` の両方**を確認する) →
      5. `coder-workspace-homes` (host `test` 925 MiB → 余力があれば host `general` 2.973 GiB)。
   - 各件で必ず: **(a)** 原本側で `find -type f | wc -l` と代表ファイルの `sha256sum`
     (稼働中 Pod への `kubectl exec`、または PVC を `readOnly` でマウントする使い捨て Job)、
     **(b)** 使い捨て PVC / emptyDir へ `restic restore latest`、**(c)** 復元側で同じ 2 つ、
     **(d)** 突き合わせ、**(e)** `date +%s` 差分または Job の age から所要時間。
   - **ファイル数は `restic ls latest` が示すスナップショット自身の件数と復元結果を突き合わせる。**
     稼働中のアプリが書き続けるので原本の実数とはズレうる。**ズレたら、ズレた事実と理由を書く。
     黙って一致したことにしない。**
   - **本番 PVC は `readOnly` マウントか `kubectl exec` の読み取りだけ。書き込みマウントをしない。**
   - **後片付け**: 使い捨て Job / PVC を `kubectl delete` し、**消したことを PROGRESS.md に書く**。
     `<app>-restic-backup-credentials` を一時的に作った場合、`autopilot-writer` は secrets を
     消せないので**残る**。残ったものは残ったと書く (P-0047 と同じ)。

4. **DoD (3) の 3 分類は「Git/B2 から戻る」「再発行が要る (誰の手で、何分)」「失うと戻らない」の
   3 つで固定する。** spec の語をそのまま使う (verify #3 が `再発行が要る` を grep する)。
   - **「再発行が要る」には必ず『誰の手で』を書く** — 人間専有 (外部サービスの管理コンソール /
     アカウント契約 / 物理作業) か、構築セッションで届くか、エージェント自身で済むか。
     CHARTER §4 の境界に照らして分ける。
   - **届かなくて確認できなかったものは「未確認」と明記する。** 推測で分類しない
     (`ops/memory/README.md` の流儀)。届かないこと自体が DR 計画の穴なので、隠すと DoD が空洞になる。

5. **`docs/disaster-recovery.md` の構成** (案。verify #2/#3 と DoD (4) を満たす最小形):
   - 前提と適用範囲 / この手順で戻らないもの (最初に書く。読む人が最初に知るべきこと)
   - **復旧手順 (上から順)**: ① node01 の再構築 (Terraform + NixOS image) → ② **sops age key と
     `doppler-token`** → ③ ArgoCD ブートストラップ → ④ ExternalSecret の同期確認 →
     ⑤ **アプリごとのデータ復元 (ここが CronJob 名を含む 5 節。テストが見る)** → ⑥ 到達性の回復
     (tailscale / Dex / ingress)
   - **RTO の実測値**: 測れたもの (restic restore) は実測秒数、測れないもの (node01 の再構築、
     ArgoCD の初回同期) は**「未実測の見積もり」と明記して分ける**。混ぜない。
   - データでない資産の 3 分類表 (DoD 3)

6. **`ops/projects/seeds.md` には末尾に追記するだけ** (DoD 4 の「次のプロジェクトの種」)。
   既存項目の並べ替え・書き換えをしない (curriculum が同じファイルを触るのでコンフリクトする)。
   番号は現在の末尾 21 の次から。

### ロールバック

追加のみの変更 (新規 2 ファイル + `docs/backup.md` への 1 行 + seeds への追記) なので、revert PR 1 本。
**クラスタ側には恒久的な変更を一切残さない** — 使い捨ての Job / PVC は自分で消す。
本番 PVC は readOnly でしか触らないのでデータは失われない。B2 側には**何も書かない**
(復元は読むだけ。`restic init` も `backup` もしない)。この 3 点を PR 本文にそのまま書くこと。

## やらないこと

- **本番 PVC への復元**。使い捨て PVC / emptyDir にしか戻さない。本番の停止・切り替えもしない。
- **実際の DR の実行** (node01 を壊す・作り直す)。今回書くのは手順と数字であって、演習は机上 +
  データ復元まで。VM 再構築の所要時間は「未実測の見積もり」と明記して書く。
- **backup / retention CronJob の変更**。schedule も保持世代も credential も参照先も触らない。
  1 PR 1 論点。
- **B2 側への書き込み** (`restic init` / `backup` / `forget` / `unlock --remove-all`)。
  今回は**読むだけ**。バケット設定・ライフサイクル・Object Lock・鍵の capability も人間専有 (CHARTER §4)。
- **Doppler の登録・変更**、新しい credential の発行依頼。**このプロジェクトに `needs-human` の
  一歩は無い**。詰まったら credential 以外を疑う。
- **PBS (qemu/112) の退役判断** (旧 T-0116)。DR の文脈で触れたくなるが別論点。気づきは seeds へ。
- **`docs/backup.md` の書き直し**。追記は「復元試験の記録が新しくなった」ことを指す 1 行まで。
- **`.github/` の変更**。既存 discover が拾う形に寄せる。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` / `ops/inventory.json` /
  CHARTER・VISION・`ops/memory/` の更新**。heart が直接 `main` に push するファイルや
  consolidation 専有の層でコンフリクトする (CLAUDE.md / `ops/memory/README.md`)。
  昇格させたい学びは PROGRESS.md に書いて consolidation に渡す。
- **`ops/rules.json` の変更**。人間レビュー必須パスであり、今回触る理由が無い。
- **使い捨ての Job / PVC を `apps/` に commit すること**。ArgoCD の prune と
  `.spec.template` immutable の両方を踏む (T-0108/T-0111)。使い捨ては `kubectl` で作って消す。

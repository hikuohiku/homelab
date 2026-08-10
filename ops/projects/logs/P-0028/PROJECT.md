# P-0028 — restic backup を append-only 鍵に切り替え、実機で backup と lock を確かめる (旧 T-0120)

## 目的

人間が 2026-08-07 に発行・登録した B2 の append-only 鍵 (`B2_ACCOUNT_ID_APPEND_ONLY` /
`B2_ACCOUNT_KEY_APPEND_ONLY`) が、登録されたまま一度も使われていない。4 つの backup CronJob は
今も削除権限つきの鍵を参照しており、node01 が侵害されればバックアップごと消せる。
「バックアップがある」と「バックアップが守られている」は別 — 壁 #6 を実効化する最終工程であり、
**homelab 本体への差分** (VISION 段階 2 の成果) にあたる。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing** (2026-08-10、`project/p-0028` の
checkout で、リポジトリルートから実行。4 本とも `rc=1` = 該当文字列なし)。

- [ ] `grep -q 'restic-backup-credentials' apps/vaultwarden/restic-backup-cronjob.yaml`
  — vaultwarden の **backup** CronJob が新しい Secret を参照していること。現在は
    L134/143/148/153 の 4 箇所すべてが `vaultwarden-restic-credentials` (削除権限つき)。
- [ ] `grep -q 'restic-backup-credentials' apps/immich/restic-backup-cronjob.yaml`
  — immich の backup CronJob。現在は L49/58/63/68 が `immich-restic-credentials`。
- [ ] `grep -q 'restic-backup-credentials' apps/coder/restic-backup-cronjob.yaml`
  — coder-postgres の backup CronJob。現在は L80/89/94/99 が `coder-restic-credentials`
    (L45 の `coder-postgres-credentials` は pg_dump 用の別物。触らない)。
- [ ] `grep -q 'restic-backup-credentials' apps/coder/workspace-home-backup-cronjob.yaml`
  — coder workspace home の**動的 Job テンプレート** (ConfigMap 内 python の
    `build_job()`)。現在は L194/207/214/221 が `coder-restic-credentials`。

**verify は「文字列がどこかに 1 回でも出るか」しか見ない。** コメントに書いただけでも、
4 箇所のうち 1 箇所だけ直しても通ってしまう。DoD が要求しているのは 4 ファイル × 4 箇所
(`RESTIC_PASSWORD` / `RESTIC_B2_BUCKET` / `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY`) の**全部**なので、
自分で `grep -c` して数を突き合わせること。切替後の期待値:

| ファイル | `-restic-backup-credentials` | 残る `-restic-credentials` (retention 用) |
|---|---|---|
| vaultwarden/restic-backup-cronjob.yaml | 4 | 4 |
| immich/restic-backup-cronjob.yaml | 4 | 4 |
| coder/restic-backup-cronjob.yaml | 4 | 4 |
| coder/workspace-home-backup-cronjob.yaml | 4 | 4 |

DoD のうち **実機実測 (4 本の手動 Job・lock の挙動) と docs/backup.md への追記は verify に
入っていない** (機械検証できない)。やらなくてよいのではなく、**PROGRESS.md と docs/backup.md が
唯一の証拠**になる。

## 設計方針

### 前提 (調べて分かったこと。すべて 2026-08-10 に実測)

- **Doppler 登録は完了している。** `kubectl get externalsecret -A` で
  `vaultwarden/immich/coder` の `<app>-restic-backup-credentials` 3 本とも
  `SecretSynced` / `Ready=True`。DoD の前提 (T-0120 の「Ready を確認する」) はここで満たされた。
  ついでに、`ops/memory/substrate.md` と CHARTER §2 が「既知事象」としている
  **3 アプリの ArgoCD `Degraded` (T-0106 由来) は解消しているはず**なので、まだ Degraded なら
  別原因。PROGRESS.md に書いて次へ渡す (このプロジェクトで追わない)。
- 新旧 Secret は `RESTIC_PASSWORD` / `RESTIC_B2_BUCKET` に**同じ Doppler キー**を使い、
  `B2_ACCOUNT_ID`/`B2_ACCOUNT_KEY` だけ `_APPEND_ONLY` 側を引く
  (`apps/*/restic-external-secret.yaml`)。よってリポジトリ・暗号化パスワードは同一で、
  **4 つの env をまとめて新 Secret に向ければよい。新旧を混在させない。**
- 変更するのは backup 側だけ。同じファイルの後半にある retention CronJob
  (vaultwarden L219-236 / immich L130-147 / coder L153-170 / workspace-home L368-385) は
  `forget --prune` に削除権限が要るので**触らない** (DoD 明記)。
- **4 本のうち `restic unlock` を実行しているのは 2 本だけ** — vaultwarden と
  coder-workspace-home (T-0108「残った stale lock が以降の実行を恒久的に失敗させた」の対策)。
  immich と coder-postgres は `restic snapshots || restic init` → `restic backup` のみ。
  4 本とも `set -eu` なので、**途中のコマンドが 1 つでも非 0 を返せば Job は Failed になる。**
- 手元の restic 0.18.1 (イメージは 0.19.1) で確認: `restic unlock` は「エラーがあれば rc=1」、
  `restic backup` は「既にロックされていれば **rc=11**」。`--no-lock` は read-only 用途向けの
  グローバルフラグで、backup で使えるかは未確認。
- 器の権限: `autopilot-writer` ClusterRole は `batch/{jobs,cronjobs}` と `pods`/`pods/log` を
  クラスタ全体で `*` 持つが、**`secrets` は持たない** (`apps/autopilot/rbac.yaml`)。
  つまり credential は必ず Pod の `secretKeyRef` 経由でしか触れない。**worker のシェルから
  restic を直接叩くことはできない** (イメージに restic バイナリはあるが鍵が無い)。
  実測はすべて「クラスタ内に Job を作ってログを読む」形になる。

### 中心の不確実性 — append-only 鍵は lock ファイルを消せない

`deleteFiles` を含まない B2 鍵では、restic の lock 削除 (`locks/` 配下の DeleteFileVersion) が
失敗する。ここが唯一の技術的な山で、DoD が「実測しろ」と言っているのもここ。**未確認の仮説を
そのまま manifest に反映しないこと。** 確かめる問い:

1. `restic unlock` は、消すべき stale lock が **0 件のときでも** 失敗するか (削除 API を
   呼ばなければ rc=0 で通るはず、だが未確認)。落ちるなら vaultwarden と workspace-home が死ぬ。
2. `restic backup` が自分で張った lock を終了時に消せないとき、restic は警告だけ出して rc=0 で
   終わるか、それとも非 0 か。
3. 長時間の backup (immich が該当) は lock を定期 refresh する。refresh は「新 lock を作って
   旧 lock を消す」なので、消せないと refresh 失敗と扱われ、**途中で backup 自体が中断されうる**。
4. 消せない lock が毎回積もると、次に走る **retention CronJob** (削除権限あり) が lock で
   弾かれないか。DoD の「append-only 鍵の lock の癖が実運用 (次回の unlock) を壊さないか」はここ。

### 進め方 (この順序で。飛ばすと本番の日次バックアップを止める)

1. **先にプローブする。本番リポジトリを汚さない。** writer capability で vaultwarden namespace に
   使い捨て Job を 1 個作り、`vaultwarden-restic-backup-credentials` を `secretKeyRef` で読ませ、
   `RESTIC_REPOSITORY=b2:$(RESTIC_B2_BUCKET):append-only-probe` (本番の 4 パス
   `vaultwarden`/`immich`/`coder-postgres`/`coder-workspace-homes` のどれとも違う新規パス) に対して
   **`set -eu` を使わずに** `init` → `backup` (数 KB の中身) → `unlock` → `snapshots` →
   `list locks` を順に流し、**各コマンドの rc を全部 echo する**。これで上の 1〜3 の答えが、
   本番を一切壊さずに取れる。ログは `kubectl logs job/... -n vaultwarden` で読める。
   - プローブが作った `append-only-probe` リポジトリは append-only 鍵では消せない。数 KB なので
     放置してよいが、消したくなったら削除権限つきの既存鍵を使う使い捨て Job で消す。
     **どちらにしたかを PROGRESS.md に書く** (放置するなら「意図的な残骸」と分かるように)。
2. **プローブの結果を見てから manifest を切り替える。** `restic unlock` が append-only で落ちると
   分かったら、対処は **backup CronJob のスクリプトの中だけ**で完結させる
   (例: `restic unlock || echo "append-only 鍵では lock を消せない。stale lock の除去は
   retention 側の削除権限つき鍵に委ねる"` として非致命化する)。**retention CronJob には手を
   入れない** — DoD が明示的に対象外にしている。retention 側にも手当てが要ると判断したなら、
   実装せず PROGRESS.md と docs/backup.md に「次の論点」として書いて渡す (1 PR 1 論点)。
3. PR → CI → merge → ArgoCD sync。**sync が実際に効いたことを
   `kubectl get cronjob <name> -n <ns> -o yaml | grep -A2 secretKeyRef` で確認してから**次へ進む。
   確認せずに手動 Job を起こすと、古い定義のまま「成功した」と誤認する。
4. **4 本から手動 Job を 1 回ずつ起こす。**
   `kubectl create job -n <ns> <name>-manual-20260810 --from=cronjob/<name>`
   - 4 本は別々の restic リポジトリパスなので並行して構わない。ただし
     **スケジュール実行 (JST 02:45 / 03:10 / 03:30 / 03:40、retention は日曜 04:00〜04:30) と
     重ならない時間帯に打つ。** CronJob の `concurrencyPolicy: Forbid` は手動 Job には効かない。
   - `coder-workspace-home-backup` はオーケストレータで、本体の restic は子 Job
     (`chb-<workspace-id>`) が動かす。**追うべきログは子 Job のほう。**
     子 Job は `ttlSecondsAfterFinished: 3600` で消えるので、**ログを取り切る前に消える**。
     同名の子 Job が残っていると 409 でスキップされる (エラーにならず「作った」と見える) 点にも注意。
   - 手動 Job は ArgoCD の管理外 (ラベルが付かない) なので prune されない。**終わったら自分で
     `kubectl delete job` する。**
   - 各本について、rc・所要時間・`restic backup` の出力 (追加されたスナップショット) と、
     **lock の残骸が出たか**を記録する。
5. **`docs/backup.md` に追記する。** 場所は「backup 専用 credential への分離 (T-0106, 2026-08-06)」
   節の直後に、切替の節を足す。既存の「登録後の切り替え (T-0120, blocked)」の記述は実施済みの
   事実に書き換える (放置すると実態とドキュメントが乖離する — CHARTER §1)。書く内容は
   **何をどう切り替えたか / 4 本の実測結果 / append-only 鍵の lock の癖と次回 retention で何が
   起きるか / 戻し方**。推測は「未確認」と明記して混ぜない。

### ロールバック

`secretKeyRef.name` を元に戻す revert PR 1 本。**データは失われない** — 同じバケット・同じ
リポジトリパス・同じ `RESTIC_PASSWORD` なので、append-only 鍵で書いたスナップショットは
既存の削除権限つき鍵からも普通に読める。消せずに積もった lock は、削除権限つき鍵で
`restic unlock --remove-all` を打つ使い捨て Job を作れば手で片付けられる (retention の
manifest は変えずに済む)。この 2 点を PR 本文のロールバック手順にそのまま書くこと。

## やらないこと

- **retention CronJob の変更** (credential 参照もスクリプトも)。`forget --prune` に削除権限が
  要る。DoD が明示的に対象外にしている。
- **`apps/*/restic-external-secret.yaml` の変更。** 3 本の ExternalSecret は既に存在し
  `SecretSynced`。参照する側を向け替えるだけ。
- **既存 `<app>-restic-credentials` / 旧 B2 鍵の削除・失効。** retention が使い続けるし、
  切替が実測で安定するまで戻し先として残す (「戻せる形」の維持)。
- **B2 側の設定** (バケットのライフサイクルルール、Object Lock、鍵の capability 変更)。
  管理コンソール操作は人間専有 (CHARTER §4)。append-only 鍵の capability が本当に
  `deleteFiles` 抜きかは、プローブの実測結果から**推定するだけ**にとどめる。
- **restic イメージタグの更新** (`ops/check_version_sync.py` の GROUP
  「restic/restic backup CronJob image tag」の対象)。1 PR 1 論点。
- **`ops/backlog.json` の T-0106 / T-0120 の status 更新。** heart が直接 `main` に push する
  ファイルでコンフリクトしやすい (CLAUDE.md)。**PROGRESS.md に「T-0120 は P-0028 で消化した」と
  書いて次へ渡す。**
- **復元試験のやり直し。** DoD は backup 成功と lock の挙動まで。restore は T-0071 で 4 対象とも
  完了済みで、読み出しは削除権限つき鍵で行うため鍵の分離とは独立した論点。
- **バックアップの健全性監視の作り込み** (ops-health-reporter への項目追加など)。
  今回の実測は手動 Job のログで足りる。仕組み化したくなったら別プロジェクト。

# P-0028 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

受入 **4/4 green**（2026-08-10、自分で実測）。manifest の切り替えとプローブ実測、
`docs/backup.md` への追記まで完了。**残るのは merge → ArgoCD sync 後の手動 Job 4 本**
（DoD のうち verify に入っていない部分。下の「次のセッションへ」参照）。

## やったこと（セッション 1、2026-08-10）

1. **プローブで append-only 鍵の挙動を実測した**（本番リポジトリは汚していない。
   使い捨てパス `append-only-probe` を使用）。結論は下の実測ログ。
2. **4 ファイルの backup CronJob の credential 参照を切り替えた**
   （各 4 env = `RESTIC_B2_BUCKET` / `RESTIC_PASSWORD` / `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY`）。
   retention CronJob 4 本は無変更（各ファイルに `name: <app>-restic-credentials` が 4 行残る
   ことを確認済み）。`apps/*/restic-external-secret.yaml` も無変更。
3. 切り替えの理由を各 manifest にコメントで残し、`docs/backup.md` に節
   「append-only 鍵への切り替え (T-0120 / P-0028, 2026-08-10)」を追加。
   既存の「登録後の切り替え（T-0120, blocked）」は実施済みの事実に書き換えた。
4. `kubectl kustomize apps/vaultwarden` / `apps/coder` が通ること、
   埋め込み python の `build_job()` が生成する Job の `secretKeyRef` が
   `coder-restic-backup-credentials` のみになることを実際に評価して確認した。
   （`apps/immich` の kustomize は helm 未インストールでこの環境では検証不可。
   変更内容は YAML コメント + Secret 名のみ）

## 実測ログ (プローブ / 手動 Job)

すべて 2026-08-10、vaultwarden namespace の使い捨て Job。実行後に削除済み。

### 1. 鍵の capability（B2 `b2_authorize_account` を直接叩いた）

| 鍵 | capabilities |
|---|---|
| backup 用（`*_APPEND_ONLY`） | `listBuckets` `listFiles` `readFiles` `writeFiles` — **`deleteFiles` なし** |
| retention 用（既存） | 上記 + `deleteFiles` + バケット設定系一式 |

人間の発行内容は依頼どおり、**本物の append-only 鍵**だった。keyID も別物（同一鍵の使い回しではない）。

### 2. 中心の不確実性の答え — **lock は壊れない**

PROJECT.md が「唯一の技術的な山」としていた 4 つの問いは、**すべて杞憂だった**。
使い捨てリポジトリで append-only 鍵だけを使い、`init` → `unlock`(lock 0 件) → `backup` →
`list locks` → `backup` → `list locks` → `unlock` → `unlock --remove-all` → `snapshots` を
流した結果、**全コマンド rc=0、`list locks` は毎回空**。

- Q1「lock 0 件の `unlock` は落ちるか」→ **落ちない（rc=0）**
- Q2「backup が自分の lock を消せないと非 0 か」→ **消せている（rc=0、残骸なし）**
- Q3「長時間 backup の lock refresh が失敗して中断するか」→ **削除が成功する以上、起きない**
- Q4「消せない lock が積もって retention を弾くか」→ **積もらない**

**理由（ここが本質）**: restic の B2 backend は削除に `b2_delete_file_version` ではなく
**`b2_hide_file`** を使える。`b2_hide_file` は `writeFiles` だけで通るので、`deleteFiles` の
無い鍵でも「restic から見た削除」は成功する。`b2_list_file_versions` で裏を取った:

- append-only 鍵で消した lock / snapshot → `action=hide` のマーカーが増え、元の `action=upload`
  の版はバケットに残る
- 削除権限つき鍵で `forget --prune` → 版そのものが一覧から消える（hide マーカーも増えない）

**よって backup スクリプト（`restic unlock` を含む）には一切手を入れていない。**
PROJECT.md が示唆していた `restic unlock || echo ...` の非致命化は**不要**。

### 3. 本番 4 リポジトリでの事前確認

append-only 鍵で `vaultwarden` / `immich` / `coder-postgres` / `coder-workspace-homes` の
4 パスに `snapshots` / `list locks` / `unlock` を打った → **4 本とも rc=0、残留 lock 0 件**。
（書き込みはしていない。本番リポジトリにゴミのスナップショットを作らないため）

### 4. バケットの設定

`lifecycleRules=[]`（空）、`bucketType=allPrivate`、`fileLockConfiguration` は読めず（未設定と思われる）。
→ hide された版が自動で完全消滅することはない。**この空のライフサイクル規則が append-only 鍵の
防御の前提**。誰かが「最新版のみ保持」等を入れると防御が無効になる（docs/backup.md に明記した）。

### 5. 残骸（意図的）

使い捨てプローブが作った `b2:<bucket>:append-only-probe`（数 KB、スナップショット 1 本 +
hide マーカー）は**残してある**。append-only 鍵では消せず、削除権限つき鍵でプレフィックス配下を
全消しするスクリプトを書くのは本番データへの誤爆リスクに見合わないと判断した。
docs/backup.md の「残骸」節に記載済み。

## 発見 (このプロジェクトの外へ渡すもの)

- **T-0106 由来の ArgoCD `Degraded` は解消している。** 13 アプリすべて `Synced` / `Healthy`
  （2026-08-10 実測）。`ops/memory/substrate.md` と CHARTER §2 が「既知事象」として
  抱えている記述は**もう実態と合っていない**。棚卸しの対象。
- **T-0120 は P-0028 で消化した。** `ops/backlog.json` の status 更新は heart の領分なので触っていない。
- **append-only 鍵は「恒久削除」は防ぐが「見えなくする」ことは防げない。** 攻撃者は hide で
  バックアップを restic から消えたように見せられる。復旧には削除権限つき鍵かマスターキーで
  hide マーカーを消す作業が要る（= 復元までの時間は奪われる）。完全な不変性が要るなら
  B2 の Object Lock が必要で、これは人間の管理コンソール作業。**別プロジェクトの種**。
- **retention の `forget --prune` は削除権限つき鍵なので実削除している**（実測）。
  B2 のストレージは正しく回収されている。
- ArgoCD MCP はこのヘッドレス起動では `ARGOCD_API_TOKEN` 未設定で使えなかった
  （`Missing required ArgoCD API token`）。ArgoCD の状態確認は `kubectl get applications -n argocd`
  で代替した。CLAUDE.md は「参照は MCP 原則」としているが、定期実行では MCP が使えない場合がある。
- `apps/immich` の `kubectl kustomize` は helm バイナリが要る（`--enable-helm` でも
  `helm: executable file not found`）。worker 環境では immich の kustomize 検証ができない。

## 次のセッションへ

**受入 4 項目は全部 green。残りは DoD の「実機で 4 本の手動 Job」だけ。**
これは merge → ArgoCD sync の後でないと意味がない（古い定義のまま起こすと誤認する）。手順:

1. `kubectl get cronjob <name> -n <ns> -o yaml | grep -A2 secretKeyRef` で
   **`<app>-restic-backup-credentials` に変わっていることを先に確認する。**
   対象: `vaultwarden-restic-backup`(vaultwarden) / `immich-restic-backup`(immich) /
   `coder-restic-backup`(coder) / `coder-workspace-home-backup`(coder、ConfigMap 側)
2. `kubectl create job -n <ns> <name>-manual-<date> --from=cronjob/<name>` で 4 本。
   **スケジュール（JST 02:45 / 03:10 / 03:30 / 03:40、retention は日曜 04:00〜04:30）と
   重ならない時間帯に。** `concurrencyPolicy: Forbid` は手動 Job には効かない。
3. `coder-workspace-home-backup` はオーケストレータ。**追うのは子 Job `chb-<workspace-id>` の
   ログ**。子は `ttlSecondsAfterFinished: 3600` で消えるので取り切る前に消える。
   同名の子が残っていると 409 でスキップされる（エラーにならず「作った」と見える）。
   現在の workspace は 2 つ（`general` / `test`）。
4. 終わったら `kubectl delete job` する（手動 Job は ArgoCD 管理外なので prune されない）。
5. 結果（rc・所要時間・追加されたスナップショット・lock の残骸の有無）をこの PROGRESS と
   `docs/backup.md` に追記する。

**罠**: プローブで確認済みなので手動 Job は成功するはずだが、vaultwarden と coder-postgres は
initContainer（sqlite コピー / pg_dump）を持つ。そちらの失敗は credential 切り替えとは無関係。
ログはコンテナを指定して読むこと（`-c restic-backup`）。

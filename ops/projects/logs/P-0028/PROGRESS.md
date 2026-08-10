# P-0028 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

受入 **4/4 green**。**DoD もすべて充足した**（セッション 2 で実機実測を完了）。
レビュー指摘 4 件はすべて解消済み。**このプロジェクトでやり残していることは無い。**

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

## やったこと（セッション 2、2026-08-10 — レビュー指摘 4 件の解消）

前回のレビューは 4 件差し戻した。**全部つぶした。**

### 指摘 1: DoD 後半（実機の手動 Job 4 本）が未実施 → **実施した**

前セッションの「merge 後でないとできない」は誤り。レビューの指摘どおり `preview` で
merge 前に実クラスタへ流せる。**注意: この worker 環境に `just` バイナリは無い**（`command not found`）。
justfile の `preview` レシピと等価な `kubectl patch` を直接打った:

```
kubectl patch application apps -n argocd --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl patch application <app> -n argocd --type merge -p '{"spec":{"source":{"targetRevision":"project/p-0028"}}}'
# 戻すとき: targetRevision を HEAD に戻し、apps の syncPolicy.automated を {prune:true,selfHeal:true} で復元
```

手順と結果:

1. vaultwarden / immich / coder の 3 Application を `project/p-0028` に向け、
   `argocd.argoproj.io/refresh=hard` で強制リフレッシュ → 3 本とも rev `2f4a766d` に Synced。
2. **live の CronJob を先に確認**（切替前は 4 本とも旧 Secret だったことも記録済み）。
   切替後は backup 3 本が `<app>-restic-backup-credentials`、retention 3 本は
   `<app>-restic-credentials` のまま、workspace-home の ConfigMap も新 Secret 4 箇所。
3. 04:03 UTC（13:03 JST、夜間スケジュールと無衝突）に手動 Job を 4 本。**6 本すべて rc=0**
   （オーケストレータの子 Job 2 本を含む）。所要 24〜36 秒。詳細な表は `docs/backup.md` に。
4. 直後に append-only 鍵で 4 リポジトリに `restic list locks` → **4 本とも rc=0・出力 0 行**。
   スナップショットも 4 パスすべてに増えている（`vaultwarden` 7 / `immich` 7 /
   `coder-postgres` 7 / `coder-workspace-homes` 4）。
5. 手動 Job・子 Job・検証用 Job をすべて削除。**preview を reset して root `apps` の
   auto-sync を復元済み**（全 13 アプリ Synced/Healthy、CronJob は `main` の旧 Secret 参照に戻っている
   = 未 merge なので正しい状態）。

**新しく分かった実測事実**（前セッションが推定にとどめていた部分）:

- `b2_list_file_versions` で見ると、**バケット内の hide マーカー 18 件はすべて 2026-08-10
  03:51〜04:05 UTC のもの**。それ以前（削除権限つき鍵だけで日次バックアップを回していた期間）の
  hide マーカーは **1 件も無い**。「append-only 鍵の削除 = hide / 削除権限つき鍵の削除 = 実削除」が
  本番でもそのまま現れた。前セッションの推論の直接証拠。
- **既知の癖（無害）**: 子 Job `chb-7fdb7787…` のログに
  `Load(<lock/d4b32b1c2b>) failed: b2_download_file_by_name: 404` が出た。直前の Job が hide した
  lock が一覧 API にはまだ見えるのに download が 404 になる一瞬のずれ。**restic は警告のみで
  続行し rc=0 で完走**する。次にこのログを見た人が事故と誤認しないよう docs に明記した。
- **副作用**: append-only 鍵では lock 除去が hide になるため、lock の旧版 + hide マーカーが
  永久に積もる（1 日 6 版程度 × 200 バイト弱 = 年 1 MB 未満）。`forget --prune` は restic から
  見えないこれらを回収しない。容量は無視できるので放置の判断。

### 指摘 2: 実測より強い主張が manifest / docs に残っている → **測った範囲だけに直した**

指摘のとおり、前セッションが「本番 4 リポジトリで rc=0」と書いたときに本番で通していたのは
`snapshots` / `list locks` / lock 0 件の `unlock` だけだった。今回、本番で書き込みと lock 除去を
実際に通したので、**主張を弱めるのではなく実測で裏付ける形で解消**した。
`docs/backup.md` の実測節を **(a) 使い捨てリポジトリ / (b) 本番の読み取り確認 / (c) 本番 4 本の
手動 Job** の 3 つに分け、どれをどこで測ったかが読み取れるようにした。
`apps/vaultwarden/restic-backup-cronjob.yaml` と
`apps/coder/workspace-home-backup-cronjob.yaml` のコメントも「切替後の本番リポジトリで
手動 Job を起こして rc=0・残留 lock 0 件を実測」に具体化した。

### 指摘 3: 切替と矛盾する古い記述が 4 か所 → **4 か所とも直した**

- `apps/{vaultwarden,immich,coder}/restic-external-secret.yaml` の backup 用 ExternalSecret:
  「参照する CronJob はまだ無いので日次バックアップに影響しない」→ **逆の警告**に書き換えた。
  「この ExternalSecret は日次バックアップの単一障害点。消すと 4 本の backup が止まる」。
  将来 Doppler キーや ExternalSecret を消す事故の予防が目的。
- 同ファイルの削除権限つき側のコメント「登録が完了し次第 T-0120 で切り替える」→
  「P-0028 (T-0120) で切り替え済み。この Secret を使うのは retention だけ」に修正。
- `docs/backup.md` の T-0106 節「現状は追加のみで…まだ切り替えていない」→
  当時の記述だと明示したうえで「もう当てはまらない」と書き、新しい節へのポインタにした。
- `docs/backup.md` の Doppler 表の `B2_ACCOUNT_ID` の説明も「backup と共用」→「retention 専用」に修正。

### 指摘 4: B2 の残骸を人間に押し付けている → **消した**

`b2:<bucket>:append-only-probe`（**18 版 / 3354 バイト**）を削除権限つき鍵を使う使い捨て Job で
削除した。二重の誤爆防止をかけた:

1. 削除対象は `append-only-probe/` で**前方一致する版だけ**。本番の 4 パス
   (`vaultwarden/` `immich/` `coder-postgres/` `coder-workspace-homes/`) のいずれとも前方一致しない。
2. 削除ループ内でも 1 版ごとに `assert name.startswith(PREFIX)`。

削除前に全プレフィックスの版数を記録し（probe 18 / coder-postgres 38 /
coder-workspace-homes 129 / immich 46 / vaultwarden 38）、削除後に probe 配下が 0 版になったことを
確認した。**バケットに autopilot が片付けられない残骸は無い。**

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

（セッション 2 で追加）

- **worker 環境に `just` バイナリが無い。** CLAUDE.md と justfile は `just preview` を
  唯一の手順として書いているが、ヘッドレスの worker からは使えない。等価な `kubectl patch` を
  手で打つ必要がある（上に記載）。**`just preview` は root `apps` の auto-sync を消すので、
  戻し忘れるとクラスタが宣言的でない状態のまま放置される。** worker 向けに
  「`just` が無い環境での preview 手順」を CLAUDE.md か justfile のコメントに書くか、
  `just` を worker イメージに入れるのが望ましい。**別プロジェクトの種。**
- **日次 backup が毎回フルスキャンしている（parent snapshot が効いていない）。**
  vaultwarden / immich / coder-postgres の手動 Job のログはいずれも
  `no parent snapshot found, will read all files` だった。restic の親選択は既定で
  `host,paths` グループなので、**Job ごとに変わる Pod 名がホスト名になり毎回別グループ扱い**に
  なっているのが原因と思われる（未確認の推定）。データは内容アドレスで重複排除されるので
  B2 の容量とアップロード量は正しく抑えられている（immich は 340 MiB 読んで 1.8 MiB 追加）が、
  **毎回全ファイルを読む I/O は無駄**。workspace home の子 Job だけは
  `using parent snapshot` が出ており、こちらは効いている（`--host` か `--group-by` の
  指定差と思われる）。`--host` を固定すれば揃うはず。**別プロジェクトの種。**
- **B2 の hide マーカーは今後ずっと積もる。** 気にするなら B2 のライフサイクル規則を
  `<path>/locks/` プレフィックスに限定して入れる手はあるが、**規則の範囲を誤ると
  append-only の防御そのものが無効になる**（`docs/backup.md` に警告済み）。
  管理コンソール作業なので人間の領分（CHARTER §4）。容量は年 1 MB 未満なので急がない。
- **完全な不変性が要るなら B2 の Object Lock**（`fileLockConfiguration` は現状読めず未設定と思われる）。
  append-only 鍵は「恒久削除」は防ぐが「hide で見えなくする」ことは防げない。人間の
  管理コンソール作業。**別プロジェクトの種**（セッション 1 の発見の再掲）。

## 次のセッションへ

**やることは無い。** 受入 4/4 green、DoD の実機実測も完了、レビュー指摘 4 件も全部解消した。
次のセッションが起きたら、まず `git log` と wrapper の実測結果を見て、
**本当に差し戻されているのか（新しいレビュー指摘があるのか）を確認すること。**
無ければ何も足さない。**この PR に論点を追加しない**（1 PR 1 論点）。

もし新しい指摘で実機の再確認が要るなら、上の「指摘 1」の手順をそのまま再実行できる。
そのときの罠を 3 つ残す:

- **`just` は無い。** `kubectl patch` を直接打つ（コマンドは上に記載）。
  **終わったら必ず preview を reset して root `apps` の auto-sync を戻す。**
  戻し忘れるとクラスタが宣言的でない状態で放置される。
- **子 Job `chb-<workspace-id>` は固定名。** `ttlSecondsAfterFinished: 3600` で消えるまでに
  ログを取ること。前回分が残っていると 409 でスキップされ、**エラーにならず「作った」と見える**。
  現在の workspace は 2 つ（`general` / `test`）。
- **vaultwarden と coder-postgres は initContainer**（sqlite コピー / pg_dump）を持つ。
  そちらの失敗は credential 切り替えとは無関係。ログは `-c restic-backup` を付けて読む。

**未 merge なので、いまクラスタで動いているのは `main` の定義（旧・削除権限つき鍵）。**
切り替えが実際に効くのは merge → ArgoCD sync の後。それ自体は wrapper と heart の領分。

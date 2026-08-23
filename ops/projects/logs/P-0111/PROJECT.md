# P-0111 — 鍵は登録されたのに Degraded が消えない — coder と immich を 16 日目に Healthy へ返し、「自然解消する」と言われていた根拠を root_cause.md で訂正または確認する

## 目的

coder / immich の ArgoCD `Degraded` は T-0106 由来の既知事象で「append-only 鍵の Doppler 登録
(2026-08-07 済み) により自然解消する」と記録されてきたが、鍵登録から 15 日経過しても解消していない。
注記が誤っているか、別の破れが重なっている。vaultwarden だけはいつの間にか Healthy に戻っており、
3 つのうち 2 つだけ残る現状は「既知だから見ない」が定着した証拠。人間が開く ArgoCD/health の赤を
消すのは homelab 本体の修繕である。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-22、`project/p-0111` の checkout で、リポジトリルートから実行)。

- [ ] `test -s ops/projects/logs/P-0111/root_cause.md`
  — 一次原因を特定して root_cause.md に書けていること (空ファイル不可)。
  実測 rc=1 (ファイル自体が未存在)。
- [ ] `git fetch origin ops-health-report -q && git show origin/ops-health-report:ops/health/latest.json | python3 -c "import json,sys; d=json.load(sys.stdin); h={a['name']:a['health'] for a in d['applications']}; assert h.get('coder')=='Healthy' and h.get('immich')=='Healthy'"`
  — ops-health-report の latest.json で coder と immich が**ともに** `Healthy` になっていること。
  実測 rc=1 (AssertionError。実値は coder=Degraded / immich=Degraded / vaultwarden=Healthy)。

**verify は DoD の下限であって DoD そのものではない。** spec の dod どおり、root_cause.md は
「Degraded の一次原因を (ExternalSecret の SecretSyncedError の有無・イベントログ・Doppler キー名と
の突合) 特定し、**実名で**」書くことが本体で、verify #1 は「ファイルが空でない」しか見ていない。
Git で治るものは manifest を直し、クラスタ側でしか治らないものは最小の手順を残す。
substrate.md の T-0106 注記を観測結果に合わせて訂正することも dod だが verify が見張っていない —
PROGRESS.md に証跡を残す。

## 設計方針

### 前提 (initializer が 2026-08-22 に実測・実読した。調べ直さなくてよい)

- Degraded の温候補は `<app>-restic-backup-credentials` ExternalSecret (各 namespace 1 本):
  `apps/coder/restic-external-secret.yaml` と `apps/immich/restic-external-secret.yaml` に実在し、
  Doppler キー `B2_ACCOUNT_ID_APPEND_ONLY` / `B2_ACCOUNT_KEY_APPEND_ONLY` を参照する
  (T-0106 で分離された append-only 鍵。manifest コメントには「2026-08-07 に人間が登録済み、
  capabilities は 2026-08-10 に実測」とある)。vaultwarden にも同型がある
  (`apps/vaultwarden/restic-external-secret.yaml`)。
- **vaultwarden だけは現在 Healthy** (latest.json 実測)。同型の ExternalSecret を持つ 3 アプリの
 うち 1 つが自然回復し 2 つが残った — この差分が最強の手がかり。manifest 上の参照キー名・
 構造は見た目ほぼ同一なので、差分はクラスタ側の状態 (Secret 同期状態・イベント・Doppler 実値) か
 manifest の僅かな違いにあるはず。**まず vaultwarden (正常系) と coder/immich (異常系) を並べて
 比較するのが調査の出発点**。
- CHARTER §2 は「T-0106 の Doppler 登録が完了し ExternalSecret が `SecretSynced` になれば、
 この 3 Application の health は自然に `Healthy` へ戻るはずで、戻らなければそちらを調査する」
 と予言しており、本プロジェクトはその「そちら」。
- 訂正対象の注記は `ops/memory/substrate.md` 末尾 (L144-145): 「coder / immich / vaultwarden の
 ArgoCD `Degraded` は T-0106 …由来源の既知事象。新規異常ではない。鍵が登録されれば自然解消する —
 verified_at: 2026-08-06」。memory/ の書き手は本来 consolidation の PR のみだが、spec の dod が
 名指ししているので worker が直接訂正してよい (P-0015/P-0026/P-0101 の前例どおり)。
- capabilities に `kubectl-write` がある。診断 (read) だけでなく、ExternalSecret への再同期指示等の
 最小の write は kubectl CLI で実施できる (MCP は read-only のため CLI を使うのが CLAUDE.md の流儀)。
- latest.json の取得は `git fetch origin ops-health-report` + `git show` がこの環境で実測成功
 (shallow clone の refspec 罠 — substrate.md git 節 — に今回の verify は明示 refspec で回避済み)。

### 決めてあること

- **診断が先、修繕は後。** `kubectl get externalsecret -A` → 対象 3 ns の describe/events →
 remoteRef キー名と Doppler 実キーとの突合、の順で証拠を集めてから root_cause.md を書く。
 推測を実名で書かない。「SecretSyncedError が `<app>-restic-backup-credentials` だけか、別の
 リソースが親 Application の health を引き上げているか」も最初に潰す (CHARTER §2 の確認手順を踏襲)。
- root_cause.md は実名 (namespace / resource 名 / Doppler キー名 / イベントの実文言) で、
 「一次原因」「なぜ 15 日解消しなかったか」「vaultwarden との差分」「修繕経路」を持つ形にする。
- 修繕は Git で治るもの (manifest の誤り等) なら PR で直し、クラスタ側でしか治らないもの
 (再 sync 待ち・annotation での force refresh 等) なら kubectl-write で実施し、手順を
 root_cause.md に最小形で残す。**Doppler 側の鍵発行・変更は人間専有** — 診断がそこに落ちたら
 needs-human 化で最小の依頼文言まで詰める (T-0106 が 14 番目まで行列に入った教訓:
 review-log.md。依頼には health への言及を添える)。
- 完了判定の latest.json は ArgoCD の反映待ちがあるので、manifest/cluster 修正後に
 「Healthy へ変わるまで待つ」工程を見込む。変わらないなら原因特定が誤っていたことになる —
 root_cause.md に戻って訂正する (verify #2 が唯一の完成判定)。
- substrate.md の訂正は観測結果が確定してから最後にやる。先に書き換えない。

## やらないこと

- **Doppler の B2 鍵の発行・ローテーション・capabilities 変更**。物理作業を伴う人間専有
 (CHARTER §4)。必要になったら needs-human 化して依頼文言を整えるところまで
- **backup CronJob / restic リポジトリ自体の触り**。バックアップの成否は本論点の外
 (1 PR 1 論点。`<app>-restic-backup-credentials` は日次 backup の単一障害点という注意書きが
 manifest にあるので、write するときは影響範囲を先に読む)
- **他 11 アプリの ExternalSecret パターンの整理・リファクタ**。coder / immich の Healthy 復帰に
 必要な最小差分のみ
- **ops/backlog.json / ops/state.json / ops/journal/ の更新**。heart が直接 push する領域で
 コンフリクトする (CLAUDE.md)
- **監視・CI 配線の追加**。spec の verify に無い。「既知だから見ない」への構造的対策
 (facts.py の known-issue 扱いの見直しなど) は root_cause.md の結論を受けて別プロジェクトに分離

# P-0284 — エージェントライブに Core を映す — 常駐組は Job でなく「住人」として登録する

## 目的

人間からの直接依頼 (Telegram, request d1d085443ad3c257, 2026-08-24T06:17Z):
「ダッシュボードのエージェントライブに Core も追加して欲しい」。autopilot-core は 2026-08-23 に
納品された常駐エージェントなのに、Mission Control のエージェントライブは heart が起こす短命 Job
(label `heart/kind`) しか見ておらず、毎日見る画面に器の新しい住人が存在しないことになっている。
人間の依頼は VISION 差分より優先。ついでに「次の住人が増えるときは label を付けるだけ」の
経路を作り、個別対応を繰り返さない。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-24、`project/p-0284` の checkout のリポジトリルートで実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `cd apps/ops-dashboard/app && npx tsx --test tests/core.test.ts`
  — 常駐組 (label `heart/resident: "true"` の Deployment) が fixture JSON から snapshot に載ることを
    検証する unit test が存在し green であること (クラスタ外で回る)。
    実測 rc=1 (`Could not find 'tests/core.test.ts'`)。
- [ ] `grep -q 'autopilot-core' apps/ops-dashboard/app/src/lib/kubernetes.ts || grep -rq 'heart/resident' apps/ops-dashboard/app/src/lib/`
  — ダッシュボードの k8s 読み取り層が常駐組の列挙経路を持っていること。
    実測 rc=1 (両辺とも不成立。src/lib/ に `autopilot-core` も `heart/resident` も出てこない)。
- [ ] `grep -q 'heart/resident' apps/autopilot-core/deployment.yaml`
  — core の Deployment が常駐 label を持っていること。
    実測 rc=1 (deployment.yaml の label は `app: autopilot-core` のみ)。

## 設計方針

前提は initializer が 2026-08-24 に実読した。調べ直さなくてよい。

1. **現状の経路**: `apps/ops-dashboard/app/src/lib/kubernetes.ts` の `getKubeSnapshot()` は
   jobs / pods / `deployments/autopilot-heart` 単一 get の 3 本を取り、
   `parseKubeSnapshot(jobDoc, podDoc, deployment)` が `{ jobs, heartReady }` を返す。
   agents への展開は `snapshot.ts` の `getSnapshot()` が `kube.jobs` を `latestAction()`
   (transcript 読み) 付きで `AgentSnapshot[]` に写す。表示は `page.tsx` の `AgentCard`
   (role バッジ・podPhase 由来の LIVE ドット・経過時間)。
2. **常駐列挙は label selector の list 1 本で足す**:
   `/apis/apps/v1/namespaces/<ns>/deployments?labelSelector=heart/resident%3Dtrue`。
   **RBAC 追加は不要** — `apps/ops-dashboard/rbac.yaml` が deployments に get+list を
   既に許している (heart 表示用)。このとき `heartReady` は list 結果から
   `autopilot-heart` を引いて算出できるので、単一 get は置き換えられる。
   **その場合は `apps/autopilot/deployment.yaml` (Deployment 名 autopilot-heart) にも
   同じ label を付ける必要がある** — 付けないと heart 生死表示が消える。こちらも付けて
   「既存住人」も同一概念へ載せる (spec DoD (1) の通り)。
   label は Deployment の `metadata.labels` に付ければ selector 検索に乗る (pod template は不要だが
   付けても壊れない。worker が判断)。
3. **snapshot の形**: `KubeSnapshot` に常駐組の隣接フィールドを足し (例 `residents`),
   `getSnapshot()` が agents 配列に合流させる。常駐は projectId / transcript を持たないので
   `latestAction()` を呼ばず `transcriptAvailable: false`。Ready 状態 (readyReplicas/replicas)、
   Pod phase (list 済み pods から ownerReference か `app` label 突き合わせ)、開始時刻
   (`metadata.creationTimestamp` か status conditions) を持たせる。
   区別のため `AgentRole` 拡張か隣接 boolean のどちらかで「常駐」を表現し、`page.tsx` で
   Job カードと区別できるバッジ (「常駐」等) を出す。既存 `parseKubeSnapshot` の
   シグネチャ/戻り値変更は `tests/snapshot.test.ts` の既存テストに触る — 壊れたら
   意味を保ったまま更新してよい。
4. **テスト**: 既存パターンは `tests/snapshot.test.ts` — node:test + assert/strict,
   純関数 (`parseKubeSnapshot`) に inline fixture JSON を渡すだけでクラスタ不要。
   `tests/core.test.ts` も同型にする (実 API を呼ばない。`kubeGet` を経由しない純関数を検証対象にする)。
   実行コマンドは verify 1 の通り。

### ロールバック

revert PR 1 本で戻る。manifest 側は label 追加のみ、ダッシュボード側は表示追加のみで
データ移行・削除を伴わない。

## やらないこと

- **Core の transcript ライブ連携** (PVC 内 opencode セッションの閲覧)。spec が v1 の範囲外と明示。
  「応答可能かが一枚で分かる」まで。TranscriptViewer の拡張・SSE の常駐対応もしない。
- **RBAC の変更**。deployments list は既に許可済み。権限の追加も縮小もしない。
- **autopilot-core / autopilot (heart) の image・env・レプリカなど label 以外の manifest 変更**。
  restart-stamp の更新も不要 (pod template を触らない場合)。触るなら実再起動の覚悟で。
- **他アプリ (immich 等) のダッシュボード表示や ops-state/projects.json 周りの変更**。
  1 PR 1 論点。
- **`ops/rules.json` / `ops/backlog.json` / `ops/state.json` / CHARTER・VISION・`ops/memory/`
  の更新**。heart が直接 main に push する領域と不可侵層。
- **CI workflow (.github/workflows/) の新設**。テストは既存の discover に乗る形で。

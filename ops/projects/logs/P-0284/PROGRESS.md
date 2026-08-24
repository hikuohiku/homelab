# P-0284 — 進捗

## s1 (2026-08-24)

### やったこと (verify 3 項目すべて green 実測済み)

- `apps/autopilot-core/deployment.yaml` と `apps/autopilot/heart-deployment.yaml` の
  **metadata.labels** に `heart/resident: "true"` を追加。pod template は触っていないので
  ArgoCD sync で再起動は起きない (restart-stamp 更新不要)
- `kubernetes.ts`: `parseResidents()` (label selector list → 常駐配列) と
  `buildKubeSnapshot()` を追加。`getKubeSnapshot` は
  `/deployments?labelSelector=heart/resident=true` を 1 本増やして 4 本並列 get。
  `KubeSnapshot.residents` (id / role / replicas / readyReplicas / podPhase / startedAt)。
  Pod phase は pod template の selector label で既存 pods list と突き合わせ
  (Deployment→Pod は ReplicaSet 経由で ownerReference が直接繋がらないため)
- `snapshot.ts`: 常駐を `AgentSnapshot[]` (agents) に合流。Job カードの後ろに連結。
  `resident: true` + recentAction の位置に `Ready n/m`。transcriptAvailable は常に false
- `page.tsx` / `globals.css`: 「常駐」バッジ、role バッジに CORE/HEART 追加、
  常駐カードの時刻は経過時間でなく開始時刻 (JST) 表示
- `tests/core.test.ts` 新設 (node:test, 純関数のみ, fixture inline)。4 ケースgreen。
  既存 tests/snapshot.test.ts は無変更で green

### 設計判断と根拠

- **heartReady は単一 get (`/deployments/autopilot-heart`) を維持し、list からは算出しない**。
  PROJECT.md 設計方針 2 は「置き換えられる」としていたが、merge〜ArgoCD sync の窓で
  cluster 側の heart Deployment に label が付く前だと HEART chip が誤って「要確認」になる。
  単一 get と常駐列挙を分離しておけば故障モードも独立する。API 呼び出し 1 本増は許容
- **旧 autopilot Deployment (replicas: 0, label 無し)** には label を付けていない。
  列挙条件が label なので自然に除外される

### 発見 (スコープ外・次の curriculum 候補)

- この環境は `apps/ops-dashboard/app/node_modules` 未インストールで、`npm test` / `npm run lint`
  が `sh: tsx: not found` になる。verify だけなら `npx tsx --test ...` で回る。
  型検査は `npm ci` 後に `npm run lint` (= tsc --noEmit)
- **罠**: tsx は型検査しない。`KubeSnapshot.residents` を必須で足したとき、
  `parseKubeSnapshot` の明示戻り型注釈 (`: KubeSnapshot`) が壊れたがテストは全部 green のまま。
  `npm run lint` で初めて検出された (戻り型は `Pick<KubeSnapshot, "jobs" | "heartReady">` に変更済み)。
  型を変えたら tsx テストだけでなく lint を必ず回すこと
- 常駐カードを選ぶと TranscriptViewer は「信号待ち」の空状態になる (Core transcript 連携は
  spec により v1 範囲外)。UI 上は不都合ないが、将来 transcript を載せるなら
  `resident` フラグを見てビューア側に常駐用の空表示を用意するとより良い

### 次のセッションへの一言

verify 3 項目とも自実行で green。レビュー指摘が入ったらその解消が最優先。
型変更時は `npm ci && npm run lint` を忘れない (tsx では落ちない)。

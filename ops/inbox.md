# inbox — 実行役から計画役への引き継ぎ

実行役は作業中に気づいたことをここへ 1 行足す（backlog への起票はしない）。
計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- run #151: T-0150（python 3.14-alpine 化）の作業中、`apps/ops-dashboard/deployment.yaml` と
  `apps/coder/workspace-home-backup-cronjob.yaml` にも `python:3.12-alpine` が残っているのを見つけた。
  どちらも `ops/inventory.json` に監視対象として登録されていない（`grep -rn "python:3.12-alpine" apps/`
  で確認）。inventory へのエントリ追加＋更新タスクとして起票するか判断してほしい。

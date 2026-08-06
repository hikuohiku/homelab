# inbox — 実行役・レビュー役から計画役への引き継ぎ

実行役は作業中に気づいたことを、レビュー役はレビューで見つけたものをここへ 1 行足す
（backlog への起票はしない）。レビュー役の行には台帳の id（`R-NNN`）を必ず付ける。

計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。
`R-NNN` 付きの行を起票したときは、その task の `why` に `R-NNN` を含める（レビュー役が
`ops/review-log.md` の状態を突き合わせるのに使う）。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- [R-001] レビュー役プロンプトが指示する `kubectl -n ops-dashboard get svc` が RBAC の `services` 欠如で Forbidden になる
- [R-002] `apps/autopilot/deployment.yaml` の image digest が commit 8eacc31（chromium/フォント追加）以降更新されておらず、稼働中 Pod に chromium が無い
- [R-003] ダッシュボードの健全性サマリ「落ちている: coder、immich、vaultwarden」が T-0106 由来の無害な既知状態であることの注記が無く、初見では実障害と誤読する

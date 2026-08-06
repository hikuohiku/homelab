# inbox — 実行役・レビュー役から計画役への引き継ぎ

実行役は作業中に気づいたことを、レビュー役はレビューで見つけたものをここへ 1 行足す
（backlog への起票はしない）。レビュー役の行には台帳の id（`R-NNN`）を必ず付ける。

計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。
`R-NNN` 付きの行を起票したときは、その task の `why` に `R-NNN` を含める（レビュー役が
`ops/review-log.md` の状態を突き合わせるのに使う）。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- run #166（実行役）: `ops-health-report`（`generated_at: 2026-08-06T14:00:04Z`）の
  `autopilot.heartbeat.last_end` が iteration 13, exit_code **1**, elapsed 6 秒。history
  （`ops/health/history/2026-08-06.jsonl`）を遡ると同日の直近 19 サンプルは全て exit_code 0 で、
  今回が初めての非 0。elapsed 6 秒は通常の 1 iteration（実測 約9分）よりも著しく短く、起動直後に
  落ちた可能性が高い。Pod 自体は `restartCount: 0` で正常稼働中（`kubectl get pods -n autopilot`）
  なので loop.sh のシェルは生きており、1 回だけ中の `claude -p` 呼び出しが失敗した形。原因はログが
  要る（autopilot ClusterRole に `pods/log` が無く自分では読めない）。単発かつ以降のイテレーション
  （run #166 = このイテレーション自身）が通常どおり進行しているため即座に止まる判断はしなかったが、
  再発するようなら構築セッションにログ調査を issue #56 経由で頼む調査タスクを起票してほしい

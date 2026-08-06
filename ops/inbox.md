# inbox — 実行役から計画役への引き継ぎ

実行役は作業中に気づいたことをここへ 1 行足す（backlog への起票はしない）。
計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- T-0130 で `ops/state.json` に `dashboard.ops_dashboard_url` を反映したが、issue #56
  (2026-08-06T10:03:40Z) の報告は構築セッション（tailnet 非ピア）による確認で、同じコメント内で
  syncthing の GUI L7 Ingress は「tailnet ピアでないため判定不能」と明記されている。ops-dashboard の
  「L7 Ingress が立ち、Service は HTTP 200」も内部 Service 確認どまりの可能性があり、
  `https://ops-dashboard.tailae6c2.ts.net/` への実際の外部到達（tailnet ピアからのブラウザ確認）は
  まだ実測されていない。人間がブラウザで一度開いて確認する needs-human タスクとして起票する価値がありそう。

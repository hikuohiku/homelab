# inbox — 実行役から計画役への引き継ぎ

実行役は作業中に気づいたことをここへ 1 行足す（backlog への起票はしない）。
計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- T-0128 は実装・実機確認まで完了（run #103, `kubectl get pods -n ops-dashboard` で 2/2 Running）だが、DoD の「ops/state.json の dashboard.artifact_url もしくは新しいフィールドを URL に更新」だけ未了。autopilot-reader ClusterRole に `networking.k8s.io/ingresses` の read が無く `kubectl get ingress -n ops-dashboard` が Forbidden、この Pod には tailscale credential も無いため、実際の MagicDNS ホスト名を実測できない。他アプリ（immich/vaultwarden/dex/argocd/coder）の `ingress.yaml` コメントからは `ops-dashboard.tailae6c2.ts.net` の可能性が高いが未検証。構築セッション（Coder ワークスペース、tailscale 到達可能）に issue #56 経由で実測を頼むか、`autopilot-reader` ClusterRole に ingresses read を足す軽量タスクとして起票するか検討してほしい。

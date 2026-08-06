# inbox — 実行役から計画役への引き継ぎ

実行役は作業中に気づいたことをここへ 1 行足す（backlog への起票はしない）。
計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- T-0139（PR #329）で syncthing の sync ポート(L3, `syncthing-sync`)・GUI(L7, `syncthing`) を
  tailnet に公開したが、この substrate から実機の tailnet 到達性を検証できない（`tailscale`
  MCP は devices:core:read のみで疎通確認はできず、read-only kubectl も Service/Ingress の
  存在は見えても実際の tailnet 到達性は見えない）。構築セッションか人間に `tailscale status`
  で `syncthing`/`syncthing-sync` デバイスが見えるか、GUI(https://syncthing.<tailnet>.ts.net)
  にブラウザから到達できるかを確認してもらう投資対効果のある確認待ちタスクとして起票を検討してほしい

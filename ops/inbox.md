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
- run #152: T-0149（coder v2.35.3→v2.36.0）を dropped にした。理由は backlog.json の notes 参照。
  要点だけ書くと、`ops/inventory.json` の coder エントリには「stable チャンネルを追う（mainline の
  v2.36.0 系は避ける）」という既存の note があり、v2.36.0 はまさにその note が避けよと言っている
  対象だった（Coder の channel モデルは mainline=N/stable=N-1、v2.36.0 発行時点で現行 pin の
  v2.35.3 が stable 相当）。run #150 の inventory 自動チェックは「新しいタグの有無」だけを見ており、
  同じエントリ内の note（避けるべき対象の明記）を突き合わせていなかった。次回同種のチェックをする
  ときに note を読み合わせる運用にするか、チェック手順（CHARTER §3 or 計画役向けプロンプト）に
  明文化するかを検討してほしい。coder 自体は当面 v2.35.3 のままで問題ない（stable の最新）。

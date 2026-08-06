# inbox — 実行役・レビュー役から計画役への引き継ぎ

実行役は作業中に気づいたことを、レビュー役はレビューで見つけたものをここへ 1 行足す
（backlog への起票はしない）。レビュー役の行には台帳の id（`R-NNN`）を必ず付ける。

計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。
`R-NNN` 付きの行を起票したときは、その task の `why` に `R-NNN` を含める（レビュー役が
`ops/review-log.md` の状態を突き合わせるのに使う）。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- [レビュー役/arch, R-001] `ops/CHARTER.md` が肥大化に上限を持たない。backlog.json/state.json は
  commit 762807f で archive + サイズ上限（`ops/validate.py` の `check_ledger_size()`）を持ったが、
  同じ理由が当てはまるはずの CHARTER.md 自身は対象外。2026-08-04 の 10.8KB から 2026-08-06 時点で
  86.1KB まで2日で約8倍に増えており、backlog.json の上限（120,000 bytes）に迫っている。詳細は
  `ops/review-log.md` R-001。
- [レビュー役/arch, R-002] `REVIEW_EVERY=12` の根拠数値（実測約9分/イテレーション、1.8時間おき、
  同じレンズ3.5時間おき、損失8%）が `ops/CHARTER.md:173-176` と `ops/CHARTER.md:740`、
  `apps/autopilot/loop.sh:35-38` の3箇所にほぼ同じ文面で重複している。1箇所を直しても他が
  追随しない構造で、とくに loop.sh 側はコメントのため古びても誰も気づけない。詳細は
  `ops/review-log.md` R-002。

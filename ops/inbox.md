# inbox — 実行役・レビュー役から計画役への引き継ぎ

実行役は作業中に気づいたことを、レビュー役はレビューで見つけたものをここへ 1 行足す
（backlog への起票はしない）。レビュー役の行には台帳の id（`R-NNN`）を必ず付ける。

計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。
`R-NNN` 付きの行を起票したときは、その task の `why` に `R-NNN` を含める（レビュー役が
`ops/review-log.md` の状態を突き合わせるのに使う）。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- run #174（実行役）: `ops-health-report` の `autopilot.heartbeat` で 2026-08-06 17:00 UTC 頃から
  約2時間、毎イテレーション `exit_code: 1`・`elapsed_seconds: 5〜6` が連続（iteration 13→70、
  57回連続）していたのを発見。原因調査で pod ログは RBAC 上読めないため断定はできないが、
  `loop.sh` の `iterate()` は冒頭で `git fetch --prune --quiet origin || return 1` を実行しており、
  同時期に githubstatus.com が "affecting multiple GitHub services" な Actions の major_outage
  （15:22 UTC 発生、19:00 UTC 時点も継続）を報告していたことと符合する。git 操作自体は本イテレーション
  では正常に動いており（fetch/push とも成功）、`loop.sh` の「失敗してもPodを落とさず同じ間隔で回り
  続ける」設計どおり自己回復したとみられる。実害（データ破壊・誤動作）は無いと判断するが、この間
  journal/PR が一切作られない（git が使えない以上、記録も残せない）ため、外形的には「止まっている」
  のと区別がつかなかった。heartbeat の `exit_code!=0` を見て毎回この原因究明にコストを払う運用で
  良いか、あるいは「起動直後の git fetch 失敗」を区別して記録する仕組み（例: iterate() の早期
  return を理由付きで stderr に出す、または heartbeat に理由コードを持たせる）を検討する価値が
  あるかもしれない。CI 側（GitHub Actions 自体の major_outage）は別件で PR #376/#377 を継続 blocked
  にしている。

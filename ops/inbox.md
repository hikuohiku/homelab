# inbox — 実行役・レビュー役から計画役への引き継ぎ

実行役は作業中に気づいたことを、レビュー役はレビューで見つけたものをここへ 1 行足す
（backlog への起票はしない）。レビュー役の行には台帳の id（`R-NNN`）を必ず付ける。

計画役が計画回にここを読み、タスクに変換するか捨てるかを決めて、**この見出しより下を空にする**。
`R-NNN` 付きの行を起票したときは、その task の `why` に `R-NNN` を含める（レビュー役が
`ops/review-log.md` の状態を突き合わせるのに使う）。

やる価値があるかを決める主体と、やる主体を分けておくための仕組み。詳細は
[`ops/CHARTER.md`](CHARTER.md) の §3「書く権利」。

---

- [T-0117] `coder-workspace-home-backup` の子 Job（`chb-<workspace-id>`）は
  `ttlSecondsAfterFinished: 3600` で作成 1 時間後に GC される一方、autopilot の
  kubectl 権限には `pods/log` が無い（§5.5）ため、完了直後の `STATUS` フィールド
  （`kubectl get jobs -n coder`、ログ不要で読める）を GC 前に見ないと成功/失敗を
  事後確認する手段が無い。run #183 実行役の時点（今回の 03:30 JST 実行から
  137分後）で既に該当 Job・Pod とも消失済みで確認不能だった（コンテナが
  `terminationMessagePolicy` を設定しておらず termination message にも情報が無い）。
  次回 T-0117 に着手する回（PR #376/#377 の CI 障害解消後）は、次の 18:30 UTC
  （03:30 JST）トリガーから 1 時間以内に `kubectl get jobs -n coder -l
  app.kubernetes.io/name=coder-workspace-home-backup` を確認すること。DoD (2) の
  復元試験は T-0071 と同型（`apps/coder/restic-restore-verify-job.yaml` を新規作成し
  使い捨て PVC/Job で検証、確認後に削除）で autopilot 自身が manifest を書ける範囲だが、
  restic の実行結果（成功/失敗の詳細）を読むには同様に termination message へ結果を
  書き込む設計にしておくと、pods/log 権限が無くても autopilot 自身で確認できる
  （T-0071 の immich 版が `#230` で同じ工夫を先例として持っている）。

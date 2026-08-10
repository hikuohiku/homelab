# P-0029 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## セッション記録

<!-- 1 セッション 1 ブロック。何をやったか / 分かったこと / 次への一言 を書く -->

### セッション 1 (2026-08-10) — sweep 31/31 完了、更新 PR 1 本 (#428)

**やったこと**

- `policy: auto` の **全 31 対象を 1 セッションで調べ切った**。`ops/projects/logs/P-0029/inventory-sweep.md`
  に 31 行の表として記録（未調査の行はゼロ）。verify 2 項目とも自分で実行して rc=0 を確認。
- 更新が要ったのは **`external-secrets-chart` 2.8.0 → 2.9.0 の 1 件だけ**。
  PR **[#428](https://github.com/hikuohiku/homelab/pull/428)**
  (`autopilot/P-0029-external-secrets-chart`、origin/main から worktree で作成、`/work/repo` の HEAD は
  `project/p-0029` から動かしていない)。
- 残り 30 件は「調べて据え置き」。内訳は 26 件が既に最新、4 件が新タグはあるが上げられない
  (`tailscale-operator-chart` / `k8s-nameserver` / `coder` / `gha-setup-helm-version`)。

**分かったこと（次のセッションが調べ直さなくていいこと）**

- **PROJECT.md の見立て「1 セッションで全 31 件は入らない」は外れた。** GitHub Releases API と
  Docker Hub tags API を対象ごとに 1〜2 発叩けば済み、時間を食うのは更新があった対象の
  リリースノート精読だけ。今回は更新が 1 件しか無かったので一気に終わった。
- **PROJECT.md が「17 件が CODEOWNERS 保護」と警告していた件は、今回は一度も効かなかった。**
  17 件すべてが「既に最新」で据え置きになり、出した PR は保護パスに触らない。
- **tailscale は run #41 とまったく同じ形を再発した。** git tag は `v1.102.2` まであるが、
  `tailscale/k8s-operator` / `k8s-nameserver` の実イメージは Docker Hub / GHCR とも **`v1.98.9` が最新**
  (`v1.98.10` も `v1.102.2` も 404、`stable` タグの last_updated が v1.98.9 と同時刻)。
  **git tag で判断すると必ず騙される。** 次回も実イメージの 200/404 で判定すること。
- **coder の stable/mainline はリリース本文で判定できる。** stable 版は本文冒頭が
  `> ## Stable (since <日付>)`、mainline 版は `> This is a mainline Coder release.`。
  現在 v2.35.3 が stable（2026-08-04 から）で、pin は既にそれ。v2.36.0 は mainline なので触らない。
- **`python:3.14-alpine` 系 4 エントリ (#20-23) は floating minor tag。** 3.14 内の patch は
  自動追従するのでリポジトリを触る必要が無い。動くのは 3.15 が出たときだけ（現時点で 404）。
  「最新 patch が 3.14.7 だから更新」と誤解しないこと。
- **GHA 7 エントリは major floating tag 運用** (`@v7` など)。`releases/latest` で次のメジャーの
  有無だけ見れば足りる。patch リリースは floating tag が勝手に拾う。
- 上流到達性は PROJECT.md の記述どおり全部生きていた（api.github.com / hub.docker.com / ghcr.io）。
  `gh` CLI は無いので curl + REST。`ghcr.io` は `ghcr.io/token?scope=repository:<repo>:pull` で
  匿名 token を取ってから `HEAD/GET /v2/<repo>/manifests/<tag>`。

**次のセッションへの一言**

- **DoD 上の残作業は無い見込み。** sweep 表 31/31 が埋まり、更新が必要だった唯一の対象に PR が出ている。
  次のセッションがやることは基本的に **#428 の CI 結果を見る**ことだけ。
- **#428 の CI が落ちていたら**、いちばん疑うべきは `manifest-diff` job
  (`ops/check_manifest_deletions.py`)。chart 2.9.0 の CRD render でオブジェクトが消えていた場合、
  意図的な削除なら PR 本文に `allow-delete: <Kind>/<ns>/<name>` を **バッククォート無しの地の文**で
  追記する。**本文だけ直しても CI は再実行されない**（`on: pull_request` に `edited` が無い）ので、
  `git commit --allow-empty` を push して `synchronize` を発火させること（CHARTER §4）。
- **#428 を merge しないこと。** DoD は「PR を出す」まで。auto-merge も有効化しない（設計決定 #7）。
- 罠を 1 つ回避済み: 更新ブランチは `git worktree` で作り `/work/repo` の HEAD は
  `project/p-0029` のまま。wrapper が `git push origin HEAD:project/p-0029` を無条件で打つため、
  次のセッションで追加 PR を作るときも同じやり方を守ること。

## 発見 (スコープ外。curriculum が後で拾う)

- `ops/CHARTER.md` §4 に残っている「バージョン更新の作法」が `ops/memory/` に移っていない。
  P-0029 の spec は "(ops/memory/ 参照)" と書いているが `ops/memory/` には `README.md` と
  `substrate.md` しか無い。consolidation の領分なのでここでは直さなかった。
- `policy: manual` 側で上流が動いている対象が 2 件ある（今回は spec 対象外なので PR を出していない）:
  **`argocd-chart` 9.1.6** と **`immich-postgres` 16.9-0.4.3**。特に後者は
  inventory の note が「T-0111 で initdb 経路は確立したが、16.14-1.1.1 は run #77 の実機確認で
  CrashLoopBackOff、pods/log 権限が無く FATAL 未確認」で止まったままで、
  **構築セッションへのログ確認依頼が宙に浮いている**。
- `postgres` は 18.4 / 19beta2 が出ており、`coder-postgres` (17.10) はいずれメジャー更新の判断が要る。
  inventory の note が「17→18 はデータ移行が要るので必ず人間へ」としているため今回は触っていない。
- `helm` v4 系 (v4.2.3) が出ているが `azure/setup-helm` が v3 のみ公式サポートのため
  `gha-setup-helm-version` は v3 系に据え置き（T-0118 の blocked が今も有効）。
  azure/setup-helm が v4 対応したら再検討の対象。

## 人間への依頼

<!-- 器の権限では実行できないことだけ。無ければ空のまま -->

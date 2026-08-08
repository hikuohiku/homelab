# P-0014 — 進捗

<!--
worker が毎セッション追記する。次のセッションのあなたはこれと PROJECT.md と git log しか読まない。
何をやったか / 分かったこと / 未解決の罠 / 次への一言 を残すこと。
-->

## セッション記録

### 2026-08-08 セッション 1 — 受入 3/3 green

受入 3 項目すべてを自分で実行して green を確認した。コミットは 2 本。

**`3bc7e8f` ダッシュボードの一次回答を heart 基準に据え替え (受入 1・2)**

- 旧 backlog の描画を**ページから全部落とした**。`render_queue` / `render_archive` /
  `open_tasks` / `downstream` / `wait_state` / `safe_html` / `STATUS_META` / `KIND_LABEL` /
  `RISK_LABEL` / `import ledger` を削除。跡地は `id="legacy-backlog"` の短い節
  (「凍結しました」+ seeds.md へのリンク) だけ。
- 「あなたの手が要ること」節 (`id="human-keys"`) を新設。`load_human_keys()` が
  `ops/projects/seeds.md` の `## 人間の鍵作業` 節の行頭 `- ` の行だけを採る。実測 4 件
  (T-0107 / T-0140 / T-0141 / T-0148)。lede の件数もここから数える。
- cadence / 自己状態を heart 基準に。`heart_state()` (ops-state の `heartbeat.json` の
  拍番号と鮮度) が pulse の 1 セル目。`loop_state()` は削除。masthead は
  `heart_beat_seconds()` (= `ops/heart/config.py` の既定 120 秒) から「心拍 2 分ごと」。
- `render_autopilot_self` → `render_heart_pod` に改名。正常時は反復番号を出さず
  readyReplicas だけ言う (拍番号の二重表示を消すため)。異常系 (Pod 未起動 /
  ハング / 異常終了) は残した — heartbeat.json では分からないことだから。
- build.py 冒頭の設計方針コメントを projects.json 基準に書き換え (DoD (3))。
- `id="heart-projects"` 節 (P-0001 の成果) は無傷。

**`96668cc` 配信の定期化 (受入 3)**

- `apps/ops-dashboard/build-cronjob.yaml` を新設 (`*/30 * * * *`)。
  kustomization.yaml の resources にも追加済み。`kubectl kustomize apps/ops-dashboard` が
  通ることを確認した (この環境に kubectl がある)。

### 分かったこと・罠

1. **PROJECT.md の見立ては受入 2 について不完全だった。** 「queue 節の退役でここも消える」と
   書かれていたが、**`ci.yml の manifest-diff job に1ステップ追加` は「全タスク」表からも
   出ていた**。T-0153 の `needs_human_reason` に入っており、`render_archive()` が
   `clip(note, 110)` で描いていて、禁止文字列は先頭 60 文字あたりにあるので切り落とされない。
   queue 節だけ消しても受入 2 は落ちる。だから archive 表ごと退役させた
   (DoD の「解消済みの依頼を一切表示しない」にも合う)。
2. **`--depth=1` の clone は `--single-branch` を含む。** refspec が main だけになるので、
   その後 `git fetch origin ops-state` しても `origin/ops-state` は**生えない** (FETCH_HEAD
   止まり)。`+refs/heads/ops-state:refs/remotes/origin/ops-state` と明示すること。
   mktemp の作業ディレクトリで clone → fetch → build.py まで実際に通して確認した。
3. **CronJob の command に `>-` (folded) を使わない。** より深くインデントした行が
   リテラル扱いで改行ごと残り、シェルが壊れる。`|` (literal) を使い、YAML を
   `yaml.safe_load` してから `sh -n` に食わせて検証した。
4. **build.py は実行するたびに `ops-dashboard` ブランチへ publish する** (この環境には
   `AUTOPILOT_GITHUB_TOKEN` がある)。verify を回すたびに公開ページが書き換わる。害は
   無い (次の実行で上書きされる) が、途中状態が数分公開されることは意識しておくこと。
5. **CI の `ops` job は `origin/ops-state` を持たない。** `load_*` が None を返す経路を
   monkeypatch で通して、`build()` が落ちず「観測なし」に倒れることを確認済み。
   `heart_beat_seconds()` も try/except で 120 にフォールバックする。
6. `ops/dashboard/prs.json` は build.py が書き換える追跡済みキャッシュ。セッション開始
   時点で既に dirty だった (initializer の verify 実行による)。1 本目のコミットに含めた。

### 発見 (仕様外。curriculum が拾う用)

- **`ops/journal/` (右レール「直近の当直」) が旧体制の遺物のまま。** 旧 autopilot の
  起動ごとの引き継ぎ記録で、heart には対応する書き手がいない。P-0014 の DoD は
  journal に触れていないので手を付けていないが、ページに残る「古い世界」はここ。
  heart の判断ログ (`ops/heart/` 側) に差し替えるか、節ごと落とすかの論点。
- **autopilot イメージの digest が 3 箇所 (`apps/autopilot/deployment.yaml` /
  `heart-deployment.yaml` の `image` と `AUTOPILOT_IMAGE` / 今回の
  `apps/ops-dashboard/build-cronjob.yaml`) に散った。** `ops/check_autopilot_image_pin.py`
  は deployment.yaml しか見ないので、機械検査の対象を広げないと版上げ時にずれる。
- `ops/backlog.json` の `todo` が 0 件で `ops/validate.py` が warning を出し続けている
  (backlog は凍結済みなので、この検査自体が旧体制向き)。

### 次のセッションへ

**受入は 3/3 green。実装として残っている作業は無い。** 次に起きるのは wrapper の
実測とレビュー。差し戻された場合の勘所:

- ページの見た目は実機で誰も見ていない。CSS は `.q*` と `.archive` / `.tablewrap` /
  `.filters` / `.at__*` を削り `.hk*` を足しただけで、版面 (`.grid` / `.col` / `.side`) は
  触っていない。主列が 2 節 → 3 節に増えたので、広い画面での 2 カラムの見え方が
  変わっている可能性はある (T-0148「tailnet 実到達確認」は人間の鍵作業に載ったまま)。
- CronJob は**実機で 1 度も走っていない**。ArgoCD が sync した後の初回実行で
  こけるとしたら候補は (a) イメージの pull (ghcr、public のはず)、(b) 非 root UID 10001 で
  `/work` emptyDir に書けるか (fsGroup 10001 を付けてある)、(c) `git clone` の DNS/egress。
  ログは `kubectl -n ops-dashboard logs job/ops-dashboard-build-<...>`。

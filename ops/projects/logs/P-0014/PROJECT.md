# P-0014 — ダッシュボードを heart-and-projects の真実に一致させ、配信を定期化する

## 目的

人間の実指摘 (2026-08-08)。ページの主役が凍結済みの旧 backlog (「あなたの手が要るのは 12 件」) の
ままで、内容の多くが権限開放後の現実と食い違う嘘になっている。P-0001 は「節を足す」仕様には
合格したが、**ページ全体の真実性は誰の仕様でもなかった**。あわせて配信 (build.py の publish) は
現在 runner Job の verify の副作用でしか走らず、定期性が設計されていない。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing** (2026-08-08、`project/p-0014` の
checkout で実行)。`python3 ops/dashboard/build.py` 自体は exit=0 で通り、後段の grep / test が
落ちている。

- [ ] `python3 ops/dashboard/build.py && ! grep -q '順番待ち' ops/dashboard/index.html`
  — 旧 backlog の queue 節が語ごとページから消えていること。**現在 index.html に 5 箇所ある**:
    節見出し 1 箇所 (build.py:1269 の `<h2>順番待ち</h2>`) と、**CSS コメント 4 箇所**
    (build.py:1040 / 1044 / 1080 / 1135 → index.html:123 / 127 / 163 / 218)。grep は HTML 全文に
    かかるので、節を消すだけでは通らない。CSS コメントの文言も書き換えること
    (P-0013 の verify #2 が CSS に誤一致した件と同じ罠)
- [ ] `python3 ops/dashboard/build.py && ! grep -qE 'ci.yml の manifest-diff job に1ステップ追加|FATAL エラーメッセージを確認してほしい' ops/dashboard/index.html`
  — 解消済み・凍結済みの依頼文がページに出ていないこと。この 2 文字列の出所は
    `ops/backlog.json` の needs-human タスクの `human_action.summary` (backlog.json:429 = T-0157、
    backlog.json:172 = immich-postgres の FATAL ログ調査) で、`render_queue()` がそれを
    そのまま描画している。queue 節の退役でここも消える。**backlog の要約文を別の節に
    移し替えて延命しないこと** — それだと 1 項目目だけ通って 2 項目目が落ちる
- [ ] `test -f apps/ops-dashboard/build-cronjob.yaml && grep -q 'schedule' apps/ops-dashboard/build-cronjob.yaml`
  — 配信を定期化する CronJob マニフェストが実在し、schedule を持つこと。現在
    `apps/ops-dashboard/` には application/deployment/service/ingress/external-secret/
    kustomization/server.py しかない

**1・2 項目目は `origin/ops-state` の有無に依存しない** (どちらも「消えていること」の確認)。
CI の `ops` job は build.py の exit code しか見ないので、判定は runner 側で行う。

## 設計方針

**(1) ページの一次回答を新体制に揃える — `ops/dashboard/build.py` のみを触る**

- **主役は `id="heart-projects"` 節 (P-0001 の成果) を維持する。** spec が「削って基準を満たすのは
  仕様違反」と明記している。`render_projects()` / `PROJECT_STATE_META` / `PROJECT_ORDER` は
  そのまま活かし、**旧 queue 節を退役させて主役の座を空ける**のが今回の仕事。状態語彙の単一の
  情報源は `ops/heart/statefiles.py` の `PROJECT_STATES` (9 個) のままで、DoD の
  「予告中/実行中/検品中/納品済み」は既存ラベル (`announced`/`active`/`in_review`/`delivered`) を
  指しており、改名は求められていない。
- **「直近の納品」は projects.json の `delivered` から出す。** 実測 (2026-08-08) では
  P-0004 (PR #406) / P-0001 (#408) / P-0011 (#409) の 3 件。右レール既存の「反映された変更」
  (merged PR 一覧) と役割が重なるので、**どちらか一方に寄せて二重に出さない**。
- **旧 queue 節は「凍結済みである旨 + `ops/projects/seeds.md` への案内」だけの短い節に置き換える。**
  節の中に backlog タスクの行を残さない (受入 2 項目目)。「順番待ち」の語は CSS コメントを含めて
  全廃 (受入 1 項目目)。
- **「あなたの手が要る」は `ops/projects/seeds.md` の `## 人間の鍵作業として残るもの` 節だけから
  生成する。** 現在 4 件 (T-0107 / T-0140 / T-0141 / T-0148、seeds.md:42-45)。**罠**: この 4 行の
  直後 (seeds.md:46) に、別リストの追記が漏れた番号付き項目 `14. **利用者レンズの定期検分…**` が
  紛れ込んでいる。パーサは**行頭 `- ` の行だけ**を採り、次の `## ` 見出しか非 `- ` 行で打ち切ること。
  節が見つからない・ファイルが無い場合は 0 件 (「お願いはありません」) に倒す (fail-safe)。
  `ops/backlog.json` の `needs-human` は**数えない** — 解消済みの依頼を表示しないため。
- **cadence・自己状態を heart 基準にする。** `ops-state` ブランチ直下の `heartbeat.json` は実測で
  `{"beat": 9, "at": "2026-08-08T09:59:24Z", "writer": "heart"}` の 3 キーのみ。読み方は
  `load_projects()` (build.py:118) と同型に `git show origin/ops-state:heartbeat.json` を
  try して、失敗したら `None` → その表示を諦める。現状の問題:
  - `resolve_cadence()` (build.py:838) は `state.json` の `routines` (全て `enabled: false`) →
    `in_cluster_loop.interval_human` (旧 loop.sh の「2 分間隔」) を出している。heart のビート周期は
    `ops/heart/config.py` の `HEART_BEAT_SECONDS` (既定 120 秒) が持つ
  - `loop_state()` (build.py:637) は `state.json` の `runs` 最終要素を見ており、それは
    2026-08-07T02:31:41Z (run #219) で凍結している。今このセルは常に「止まっているかも」を出す。
    `heartbeat.at` の鮮度と `beat` 番号で判定するよう差し替える
  - `render_autopilot_self()` (build.py:723) は健全性レポートの `autopilot` キー
    (P-0011 で `autopilot-heart` 対応済み) を見ており、こちらは既に heart 基準で正しい。
    差し替え後に同じ事実の二重表示にならないよう整理する
- **`TEMPLATE` は `str.format` で流し込んでいる。** CSS の `{` `}` は全て倍化されている。
  プレースホルダ (`{queue}` / `{n_queue}` / `{lede}`) を消す・足すときは `build()` の
  `TEMPLATE.format(...)` 側 (build.py:897-913) と必ず対で直す — 片方だけだと `KeyError` で
  CI (`ops` job の "dashboard build.py runs without error") が落ちる。出力は必ず `E`
  (`html.escape`) を通す。
- **build.py 冒頭の設計方針コメント (build.py:12-22) は維持し、記述だけ改める。**
  「2 問に答える」「集計バーではなく行を出す」は残す。「主役は backlog の順番待ち」と
  「数はすべてこのファイルが backlog.json から数える」を **projects.json 基準**に書き換える
  (DoD (3))。

**(2) 配信の定期化 — `apps/ops-dashboard/build-cronjob.yaml` を新設**

- schedule は 30 分毎 (`*/30 * * * *`)。既存の `apps/ops-health-reporter/cronjob.yaml` を型として
  倣う (`concurrencyPolicy: Forbid` / `restartPolicy: Never` / `successfulJobsHistoryLimit` /
  `activeDeadlineSeconds` / `securityContext` の非 root・`drop: ["ALL"]`)。`spec.timeZone` 未指定の
  schedule は **JST 評価** (`ops/memory/substrate.md`) だが 30 分毎なので実害はない。
  **memory limits は付けない** (実測の裏付けが無い。CHARTER §4 / T-0055)。
- **`AUTOPILOT_GITHUB_TOKEN` は既存 ExternalSecret を参照する** (spec 明記)。
  `apps/ops-dashboard/external-secret.yaml` が namespace `ops-dashboard` に
  Secret `ops-dashboard-github-token` (key: `token`) を作っているので、`secretKeyRef` で
  env `AUTOPILOT_GITHUB_TOKEN` に渡す。新しい ExternalSecret を起こさない。
- **build.py は git に依存する。** `load_health()` / `load_projects()` が `git show
  origin/<branch>:<path>` を subprocess で呼ぶため、**python3 と git が同じコンテナに要る**。
  init で clone → 別コンテナで実行、の分割はできない。`ghcr.io/hikuohiku/homelab-autopilot`
  は git + python3 + curl を持つ (substrate.md 実測) が、digest pin が
  `apps/autopilot/deployment.yaml` と二重管理になる (`ops/check_autopilot_image_pin.py` は
  deployment.yaml しか見ない)。採る場合は二重管理である旨をマニフェストにコメントで残すこと。
- clone は **main だけでは足りない**。`origin/ops-state` (projects.json / heartbeat.json) と
  `origin/ops-health-report` (health/latest.json) の 2 ブランチを追加 fetch しないと、
  節が出ないだけの静かな劣化になる。
- `publish()` の先 (`ops-dashboard` ブランチ) から先の経路は既存のまま動く — Deployment の
  `fetcher` コンテナが 60 秒毎に raw.githubusercontent から取得して配信している。**触らない。**
- `apps/ops-dashboard/kustomization.yaml` の `resources` に追加するのを忘れない
  (追加し忘れると verify 3 だけ通って実際には何も動かない)。

## やらないこと

- **`ops/backlog.json` / `ops/projects/seeds.md` / `projects.json` の書き換え。** backlog は凍結済みで
  案内文だけを残す対象。projects.json の書き手は heart のみ (`ops/heart/statefiles.py` の
  単一書き手の原則)。ここは全部「読むだけ」。
- **`id="heart-projects"` 節の削除・改名。** spec が明示的に禁じている。
- **`ops/dashboard/index.html` の commit。** `.gitignore` 済みの生成物 (T-0035)。CI も
  「エラー無く走る」ことしか見ていない。
- **ops-dashboard の配信経路 (Deployment の fetcher / server.py / ingress / service) の作り直し。**
  今回足すのは「生成と publish を定期実行する CronJob」だけ。
- **ダッシュボードの全面的な CSS リデザイン。** 触るのは退役する節と、その周りの文言・
  コメントに限る (1 PR 1 論点、CHARTER §4)。
- **Discord 通知 / `ops/heart/notify.py` / briefing への変更。** ここは pull 型の画面と
  その配信の話で、push 型には触れない。
- **`.github/workflows/` の変更。** 人間レビュー必須パスであり、この DoD は CI を求めていない。
- **`chores` / `last_curriculum_at` / 棄却案 (archive.jsonl の `adopted: false`) の可視化。**
  別の論点。出したくなったら別プロジェクトとして立てる。

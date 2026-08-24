# P-0272 進捗

## ログ

### 2026-08-24 セッション 1 — 実装一式 (verify 3 項目のうち 2 が self-test green、1 は環境問題で要 wrapper 実測)

**やったこと** (spec の dod 1〜3 を全部):

- `ops/tools/human_tasks.py` 新規。節抽出 (`#{1,2}` 見出しが「人間の鍵作業」を含む) +
  行パターン (`^- T-\d+:`)、`~~` 含む行は除外、番号付き行 (14.〜21.) は行頭条件で自然に弾ける。
  age_days は backlog.json の created join、欠落/形式外/未来日は 0 (最も新しい扱い)。
  出力は古い順 (age_days 降順、同点 id 昇順)。`--out` なしなら stdout。標準ライブラリのみ
- `ops/tests/test_human_tasks.py` + fixture `ops/tests/fixtures/human_tasks_seeds.md`
  (実物の節構造=番号付き混在・取り消し線 bullet を再現した断片)。10 tests green 実測
- dashboard 側: `src/lib/human-tasks.ts` (parse 純関数の TS 移植) / ops-state.ts が
  seeds.md・backlog.json の生テキストも運ぶように拡張 (loadFromGit/loadFromDirectory 両方) /
  snapshot.ts で `humanTasks` を構築 / page.tsx attention view に
  『あなたの手が要ること』セクション追加 (既存 queue-item 流用、完了報告は書き置き+#56 の案内のみ) /
  globals.css に `.human-tasks` 追加。mirror テスト `tests/human-tasks.test.ts` green 実測
  (`npm test` 10 pass、`npm run lint`=tsc クリア、`npm run build` も通過ずみ)

**verify の現在地**:

- verify 2 (`grep 鍵作業 src/`) と verify 3 (unittest): **自分で回して green 実測**
- verify 1: ツール自体は実データで動作を確認済み (4 件、全キー揃い、age_days=18 @2026-08-24)。
  **ただし `/tmp/opencode` が root 所有 drwxr-xr-x で、このコンテナの uid 10001 (autopilot)
  は書き込めない** → 指定コマンドの `--out /tmp/opencode/human-tasks.json` が
  PermissionError で死ぬ (sudo も無い)。mktemp の書き込み先に変えた同一検証では green。
  **コードのバグではない。wrapper 側で /tmp/opencode に書ける権限で実行されれば通るはず**
  (initializer の初回実測は rc=2 = ファイル未存在段階で止まっており、書き込み権限までは未検証だった)

**分かったこと / 罠**:

- `apps/ops-dashboard/app` には node_modules が無い → テスト前に `npm ci` が要る (lock は変わらない)
- parse ルールは py/ts で完全ミラー。fixture 1 ファイル共有 (py 側からは相対パス、ts 側は
  `import.meta.url` から 4 つ上のルート経由)。**fixture を変えたら両側の期待値を必ず同時に直す**
- seeds.md の『人間の鍵作業』節は末尾節だが、次の節が来ても `^#{1,2} ` で止まるようになっている。
  節内の番号付き行は旧リストの名残 (本プロジェクトでは整理しない = spec のやらないこと)

**発見 (spec 外、curriculum へ)**:

- なし今回は。強いて言えば verify 1 の固定パス `/tmp/opencode` は root 保有ディレクトリで、
  非 root runner では常に PermissionError になる環境問題。以降のプロジェクトでも
  受入コマンドにこのパスを使うなら、runner の uid 整備か書き込み可能な規約パスへの見直しが欲しい

**次のセッションへ一言**: レビュー指摘があればそれを最優先。verify 1 が権限で落ちている間は
「ツール単体の green 実測ログ (本ファイル上記) を証拠に、wrapper の実行ユーザー確認を依頼する」のが道筋。
parse 挙動を変えるときは py/ts/fixture の 3 点セットを同時に触ること。

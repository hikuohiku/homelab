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

### 2026-08-24 セッション 2 — verify 1 の環境問題を断定 (コード変更ゼロ。証跡と解消案を確定)

**状況**: 唯一の failing は verify 1、レビュー指摘は無し。このセッションで原因の切り分けを完了した。
**コードの変更は今回ゼロ** (実装はセッション 1 で完成しており、触るべき箇所が無かった)。

**断定した事実 (すべてこのコンテナでの実測)**:

- worker セッションも `uid=10001(autopilot)` で、wrapper と同一ユーザー。`sudo` は存在しない
- `/tmp/opencode` は `root:root drwxr-xr-x` (2026-08-22 07:41 作成のまま不変)。uid 10001 に
  書込権がなく特権も無いため、**この環境内では誰にも解消できない**
- verify 1 再現: `write_text` で `PermissionError: [Errno 13] ... '/tmp/opencode/human-tasks.json'`、
  rc=1 (wrapper 実測と同一トレース)。ディレクトリ書込権なしにその中へファイルを作る方法は
  OS 上存在しない → **human_tasks.py の中身に関わらず、現環境では verify 1 は絶対に green にならない**
- 一方、書き先を mktemp に変えただけの同一検証 (同一 assert 文) は **green 実測**: 4 件
  (T-0107/T-0140/T-0141/T-0148)、全キー揃い、created join も機能 (age_days=18 @2026-08-24)、
  出力順も古い順。ロジックは完成している
- verify 2・3 も本日再実測 green (unittest 10 tests OK)

**wrapper への依頼 (これで verify 1 が通る)**:

- 最小修正: runner 内で root 権限により `chown autopilot:autopilot /tmp/opencode`
  (または起動時に `install -d -o autopilot -g autopilot /tmp/opencode`)。`chmod 1777` でも可だが、
  sticky bit 下では「他 uid の残した既存ファイル」への open('w') が EACCES になる罠があるため、
  実行 uid 固定の chown の方が確実。修正時に既存の `human-tasks.json` があれば消すこと
- 代替案: 受入コマンドのみ root で実行する / 以降の spec の受入は固定パスを避ける (curriculum 議論)

**分かったこと / 罠**:

- busybox mktemp はテンプレート末尾以外の `X` を許さない (`mktemp /tmp/hoge.XXXXXX.json` は
  Invalid argument。素の `mktemp` を使う)
- verify 1 が通った後も出力ファイルは実行 uid の所有物として残る。別 uid の再実行は上書きで
  PermissionError になる → 環境修正時に一度掃除するのが安全

**発見 (spec 外)**: 前セッションの「固定パス /tmp/opencode 問題」に一般化の根拠が加わっただけで
新規なし。「受入コマンドの固定パスは実行 uid が変わると即死する」が実証された。

**次のセッションへ一言**: コードは完成済み、やることは環境確認だけ。まず `ls -ld /tmp/opencode` を見よ。
autopilot から書ける状態になっていれば verify 1 をそのまま実行して green を貼るだけでよい。
まだ root 所有なら上記の wrapper への依頼が未処理ということ (コード側にやることは無い)。
parse 挙動を変えるときは py/ts/fixture の 3 点セットを同時に。

# P-0015 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

**受入 4/4 を自分で実測して green** (2026-08-08, セッション 1)。

```
rc=0 : test -f ops/heart/adoptgate.py
rc=0 : grep -q 'adoptgate' ops/heart/reconcile.py
rc=0 : python3 -m unittest ops.heart.tests.test_adoptgate
rc=0 : grep -q 'single-branch' ops/memory/substrate.md
```

加えて CI の ops job 相当も回した: `python3 -m unittest discover -s ops/heart/tests -t .`
= **88 tests OK**、`ops/validate.py` = 0 error (warning 2 件は既存・無関係)、
`ops/check_doc_commands.py` = ok。

完成の宣言はしない (wrapper の実測が正)。

## やったこと

PROJECT.md の設計方針をほぼそのまま実装した。逸脱と判断が要った点だけ下に書く。

- **新規 `ops/heart/adoptgate.py`**
  - 純関数 `classify(results) -> {verdict, passed, broken}`。判定順は
    **broken_command > some_pass > all_fail**。timeout は独立 verdict にせず
    broken_command に畳み、理由文字列に `timeout` を残す (spec が 3 値固定のため)。
  - `describe(verdict_info)` — 差し戻し通知の本文用。どの verify が pass したか /
    どのコマンドが壊れているかを 1 行にする。
  - I/O は `run_one` / `run_verify_in(work_dir, ...)` / `clone_fresh` / `run_gate` に分離。
    テストが `run_verify_in` だけを突けるので**ネットワークに出ない**。
  - 定数は `VERIFY_TIMEOUT_SECONDS=120` / `GATE_TIMEOUT_SECONDS=300` /
    `SYNTAX_CHECK_TIMEOUT_SECONDS=30`。rules.json は触っていない (人間レビュー必須パス)。
- **`ops/heart/reconcile.py`** — `state == "proposed"` の枝にゲートを挟んだ。
  `adopt_gate` が無ければ `run_adopt_gate` action を出して proposed のまま次ビートを待つ。
  あれば `adoptgate.classify(gate["verify"])` で判定し、`all_fail` 以外は
  `_stall(..., "adopt_gate_" + verdict, "question", ...)`。**announce も veto_deadline も
  一切書かない**。breaker 中はゲート自体も走らせない。
- **`ops/heart/heart.py`** — `execute()` に `run_adopt_gate` を追加。
  `p["adopt_gate"] = {"at", "verify": 生レコード, **classify(...)}` を書き戻す
  (2 段目の `save_projects` で永続化)。audit にも verdict を残す。
- **`ops/heart/tests/test_adoptgate.py`** (15 tests) / `test_reconcile.py` に
  `TestAdoptGate` (6 tests) と `gate()` ヘルパを追加。
- **`ops/memory/substrate.md`** に `## git (clone / refspec)` 節を新設。

## 決めたこと (設計方針から踏み込んだ判断。ひっくり返すなら理由ごと)

1. **判定の正は「生レコード」で、保存された verdict ではない。**
   heart は測って `adopt_gate.verify` に生レコードを書き、reconcile は毎回そこから
   `classify()` し直す。`adopt_gate` に verdict/passed/broken も一緒に書いてあるが、
   それは projects.json を人間が読むため (DoD (2) の「理由を残す」) であって、
   状態機械はそれを信じない。判断を純関数側に集約する heart の規律に合わせた。
2. **verify が空の spec は `all_fail` でなく `broken_command`** (理由
   `no_verify_commands`)。受入基準が無い = 完成を宣言できる者が居ない。素直に書くと
   空リストは all_fail に落ちて予告まで通ってしまうので、明示的に落とした。
3. **shadow モードでもゲートを実行する。** 副作用は使い捨て clone の中の読み取りだけで
   外に出るものが無い。飛ばすと「shadow から本番に切り替えた最初のビートで未検査のまま
   予告が出る」ことになる。heart.py にコメントで理由を残した (設計方針の指示どおり)。
4. **clone に失敗したら例外を投げ、`adopt_gate` を書かない。** 空の測定を返して
   all_fail 扱いにすると、このゲートの意味がそのまま消える。`execute()` が例外を拾って
   audit に残し、次のビートでやり直す。
5. **全体タイムアウトを超えた残りコマンドは「未実行の timeout」として記録**する
   (`rc: None`)。測っていないものを「fail した」と書かない。
6. **ゲートのタイムアウト 120s は runner 側の 600s と違う。** これは意図的
   (ビートを止めない方を優先)。120s〜600s かかる verify を持つ spec は
   ゲートで broken_command になり予告されない。今のところそんな spec は無いが、
   **将来ここで理不尽に差し戻される spec が出たらこの定数を疑うこと。**

## 実測した事実 (推測で書いていない。bash 5.3.9 / git 2.54.0 / python 3.14.5)

| 事象 | rc |
|------|-----|
| `bash -n -c 'if ['` | 2 (構文エラーのときだけ非 0) |
| `bash -n -c 'no_such_cmd'` | **0** — 構文検査は存在しないコマンドを検出しない |
| `bash -c 'no_such_cmd'` | 127 |
| `bash -c 'grep -q x /nonexistent'` | **2** — 実行時の rc=2 を構文エラーと見なしてはいけない |
| `bash -c 'test -f /nonexistent'` | 1 |

`--depth=1` の罠も `file:///work/repo` を clone して再実測した。clone 直後の
`remote.origin.fetch` は `+refs/heads/project/p-0015:refs/remotes/origin/project/p-0015`
の 1 本だけで、`git fetch origin` を打っても `origin/main` は生えない。
`git fetch origin '+refs/heads/*:refs/remotes/origin/*'` を打った直後に生えた。
shallow のままでも `git show origin/main:CLAUDE.md` は成功した。

## 詰まっていること

なし。

## 発見 (スコープ外。curriculum が拾う用)

- **差し戻された spec の再採択導線が無い。** `_register_spec` は終端エントリを蘇らせないので、
  ゲートで stalled になった P-NNNN は同じ id では二度と動かない。直した spec は新しい id で
  採択される前提 (PROJECT.md「やらないこと」で明示的に対象外)。今は Discord の question を
  人間が読んで curriculum に投げ直す経路しかない。**壊れた spec を作らせない側**
  (curriculum の立案プロンプトに「新品 clone で all-fail を確認せよ」を入れる) は
  別プロジェクトの論点として残っている。
- **`ops/dashboard/build.py` を CI 相当で走らせると `ops/dashboard/prs.json` が書き換わる。**
  今回は checkout で戻したが、ローカルで CI を再現するとき毎回汚れる。
- `ops/validate.py` の既存 warning 2 件 (T-0035 の refs 切れ / backlog に todo が 0 件) は
  このプロジェクトと無関係だが残っている。

## 次のセッションへの一言

**受入 4 項目は全部 green。実装としてはやることが残っていない。**
wrapper の実測が同じ結果なら、あなたの仕事はレビュー指摘への対応だけになるはず。

レビューで突かれるとしたらここ (先回りして考えておいた):

1. **ビートが最大 300s 止まる。** heart のビートは既定 120s なので、ゲートの実行中は
   次のビートが遅れる。設計方針で「非同期化は別プロジェクト」と明示的に外してあるので、
   指摘されたら PROJECT.md の「やらないこと」を根拠に据え置いてよい。
2. **`adopt_gate.verify` の output が 1 本あたり最大 2000 字**で projects.json に載る。
   spec のレコードどおりだが、projects.json が膨らむのが嫌われたら、
   差し戻し時だけ残して all_fail のときは削る等の縮め方がある。
3. **`ops/heart/README.md` を更新していない。** 状態遷移の説明にゲートの記述が無い。
   スコープを広げない判断で触らなかったが、指摘されたら 2〜3 行足せば済む。

触ってはいけないもの (今回も守った): `ops/rules.json` / `ops/models.json` /
`ops/runner/runner.py` / `statefiles.PROJECT_STATES` / `notify.IMMEDIATE_TYPES` /
`apps/` 配下。

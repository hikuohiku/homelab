# P-0015 — PROGRESS

引き継ぎ記録。**セッションごとに追記する**。書かなかったことは次のセッションに存在しない。

## 現況

**受入 4/4 を自分で実測して green** (2026-08-08, セッション 1 / セッション 2 でも再実測)。
セッション 2 でレビュー指摘 2 件を潰した (下の「セッション 2」節)。CI 相当は **95 tests OK**
(セッション 1 の 88 から +7)。

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

---

# セッション 2 (2026-08-08) — レビュー指摘 2 件の解消

受入 4 項目は wrapper の実測でも全部 green だったので、レビュー指摘の解消だけをやった。
**新機能は足していない。前セッションの実装の 2 つの欠陥を潰しただけ。**

## 指摘 [1] rc=127 の過剰判定 — 正当な spec を恒久的に殺していた

**何が壊れていたか。** `run_one()` が `rec["not_found"] = p.returncode == 127` だけで
判定していた。bash は **2 つの別の事象に 127 を返す**:

| コマンド | rc | stderr |
|---------|-----|--------|
| `bash -c 'no_such_cmd_xyz'` | 127 | `bash: line 1: no_such_cmd_xyz: command not found` |
| `bash -c 'bash ops/drills/nope.sh'` | 127 | `bash: ops/drills/nope.sh: **No such file or directory**` |
| `bash -c './scripts/new.sh'` | 127 | `bash: line 1: ./scripts/new.sh: **No such file or directory**` |

(実測、bash 5.3.9)。**後者は「成果物がまだ無い」= 完全に正当な fail** で、まさに
all_fail に寄与すべきもの。旧実装ではこれが `broken_command` → `_stall(..., "question")`
→ **stalled は終端**で `_register_spec` が同じ id を蘇らせない、つまり人手なしには
復活しない。`ops/projects/archive.jsonl` の過去 spec 15 件のうち **P-0005 / P-0006 /
P-0010 の 3 件 (2 割) が実際にこの形** (`bash ops/drills/immich_db_restore_drill.sh` 等)。
しかも runner 側の開始前ゲートは `any(v["ok"] ...)` しか見ないので、
**ゲートが runner より厳しく、runner なら通る spec をここで殺していた**。

**直し方。** `NOT_FOUND_MARKER = "command not found"` を定数に切り出し、
`rec["not_found"] = p.returncode == 127 and NOT_FOUND_MARKER in p.stderr` にした。
そのために `stdout + stderr` を結合する前の `p.stderr` を参照している。

旧実装なら落ちることを実測で確認済み:
```
rc: 127 | not_found(new): False | not_found(old rule rc==127): True
stderr: 'bash: ops/drills/immich_db_restore_drill.sh: No such file or directory\n'
```

回帰テストを 3 本足した (`TestRunVerifyIn`):
`test_unbuilt_script_is_a_legitimate_fail` / `test_unbuilt_relative_script_is_a_legitimate_fail` /
`test_real_spec_shape_passes_the_gate` (**P-0005 の verify 列そのまま**を空ディレクトリに
通して `all_fail` を要求)。DoD (3) の「存在しないコマンド」ケースは
`no_such_cmd_xyz_p0015` のまま残してある。

## 指摘 [2] 測れないゲートが沈黙状態を作る — 見張り時限が無かった

**何が壊れていたか。** `decide()` の proposed 枝は `adopt_gate` が無い限り
毎ビート `run_adopt_gate` を出して `continue` するだけで、試行回数も時限も持たなかった。
`heart.execute()` は `run_gate()` の例外を audit.jsonl に落とすだけ (Discord 通知なし)
なので、clone 失敗・`/tmp` 枯渇・git の timeout が続くと **proposed のまま永久に
毎ビート clone をやり直し続ける**。proposed は非終端なので `non_terminal` が空にならず
`curriculum_idle` が False に固定され、**新しい立案も二度と走らない**。
ビートは回っているのに仕事が一切進まない沈黙状態。reconcile.py 冒頭の
「恒久的に黙って待つ状態を作らない」に真っ向から反しており、in_review / active / merging
が全部持っている歯止めがこの新しい待ち状態にだけ無かった。

**直し方。** 他の待ち状態に倣って `ADOPT_GATE_MAX_ATTEMPTS = 3` を
`REVIEW_TIMEOUT_HOURS` 等と同じ場所に置き、`run_adopt_gate` を出すたびに
`p["adopt_gate_attempts"]` を +1、上限に達したら
`_stall(..., "adopt_gate_unmeasurable", "incident", ...)`。

**通知型は incident。** 指摘 [1] 系の差し戻し (`question`) と型を分けたのは意図的で、
**測れないのは spec の不良ではなく仕組みの故障**だから。人間が直すべき対象が違う。

カウンタを `decide()` (純関数) 側で加算しているのは、`heart.py` が `decide()` の直後に
`save_projects` を打つため (heart.py:277)。`execute()` が例外で落ちてもカウンタは残る。

テストは `test_reconcile.py` の `TestAdoptGate` に 4 本
(`..._counts_its_attempts` / `..._handed_to_a_human` / `..._short_of_the_limit_still_measure` /
`..._ignores_the_attempt_counter`)。最後の 1 本は「測れてしまえば試行回数は関係ない」
= カウンタが正常系を妨げないことの固定。

## この 2 件から取り出した一般則 (次に同じ判断をするとき)

**このゲートの誤りは対称ではない。** 取り逃がし (broken を all_fail と判定) は
runner 側の開始前ゲートが拾い直すだけだが、誤検知 (正当な spec を broken と判定) は
終端に落として回復不能にする。**迷ったら正当な fail に倒す。**
`adoptgate.py` の docstring にこの規則を明記した。`NOT_FOUND_MARKER` が locale で
一致しなくなった場合も安全側 (正当な fail) に倒れる設計になっている。

## 実測 (セッション 2)

```
rc=0 : test -f ops/heart/adoptgate.py
rc=0 : grep -q 'adoptgate' ops/heart/reconcile.py
rc=0 : python3 -m unittest ops.heart.tests.test_adoptgate      (18 tests OK)
rc=0 : grep -q 'single-branch' ops/memory/substrate.md
```
CI の ops job 相当 `python3 -m unittest discover -s ops/heart/tests -t .` = **95 tests OK**。
`python3 ops/validate.py` = 0 error / warning 2 件 (既存・無関係)。

## 発見 (セッション 2、スコープ外)

- **bash の rc=127 が 2 義であることを `ops/memory/substrate.md` に書いていない。**
  今回は `adoptgate.py` のコメントに実測表として残すに留めた
  (`ops/memory/README.md` が「書き手は consolidation の PR のみ」としており、
  spec の DoD (4) が名指しで要求しているのは `single-branch` の 1 件だけのため)。
  次の consolidation が拾うべき事実。
- **`ops/dashboard/build.py` を CI 相当で走らせると `ops/dashboard/prs.json` が汚れる**
  (セッション 1 の発見。未解決)。

## 次のセッションへの一言

**受入 4/4 green、レビュー指摘 2 件とも解消済み。実装として残っている作業は無い。**

まだ潰していない (= 次に突かれうる) のはセッション 1 が挙げた 3 点で、いずれも
PROJECT.md の「やらないこと」を根拠に据え置いている。判断ごと引き継ぐ:

1. **ビートが最大 300s 止まる。** heart のビート既定 120s より長い。非同期化は
   PROJECT.md で明示的にスコープ外。据え置いてよい。
2. **`adopt_gate.verify` の output が 1 本あたり最大 2000 字**で projects.json に載る。
   膨らみが嫌われたら、差し戻し時だけ残して all_fail では削る縮め方がある。
3. **`ops/heart/README.md` の状態遷移の説明にゲートの記述が無い。**
   スコープを広げない判断で触っていない。指摘されたら 2〜3 行で済む。

**新しく増えた据え置き:** `ADOPT_GATE_MAX_ATTEMPTS = 3` と
`VERIFY_TIMEOUT_SECONDS = 120` は根拠のある実測値ではなく「他の待ち状態に倣った」値。
本番で理不尽な差し戻し / 早すぎる打ち切りが出たら、まずこの 2 定数を疑うこと。

触ってはいけないもの (セッション 2 も守った): `ops/rules.json` / `ops/models.json` /
`ops/runner/runner.py` / `statefiles.PROJECT_STATES` / `notify.IMMEDIATE_TYPES` /
`apps/` 配下。`ops/memory/substrate.md` は DoD (4) の範囲 (git 節) だけ触っている。

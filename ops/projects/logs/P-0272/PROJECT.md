# P-0272 — Mission Control が器の状態だけを映して依頼を映していない — 「人間の鍵作業」キューを Next.js 化で失った面に復活させる

## 目的

Mission Control は Next.js への全面書き換え (P-0088/89) で、旧 build.py 世界が持っていた
「あなたの手が要る」節 (P-0012) を失ったままになっている。seeds.md の『人間の鍵作業』
(T-0107 pveproxy 証明書 — terraform apply 凍結の直接原因 / T-0140 LXC 101 cert 取り出し /
T-0141 .envrc 重複調査 / T-0148 tailnet 実到達確認) は器からは進められない仕事で、
人間が見ない限り永遠に滞留する。VISION の接点設計は pull 型ダッシュボードに「状態」と
「判断材料」を求めており、依頼が見えない pull 型は半分死んでいる。seeds.md の節を見出しから
機械抽出し、ダッシュボードに復活させることで、項目の増減に自動追従する依頼面を作る。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-24、`project/p-0272` の checkout、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 ops/tools/human_tasks.py --out /tmp/opencode/human-tasks.json && python3 -c "import json; d=json.load(open('/tmp/opencode/human-tasks.json')); ts=d.get('tasks',[]); assert len(ts)>=4 and all(all(k in t for k in ('id','title','age_days')) for t in ts), ts"`
  — 抽出ツールが実在し、seeds.md の『人間の鍵作業』節から {id, title, age_days} を
  4 件以上 JSON として出せること。現在 4 件の T 項目が対象。
  実測 rc=2 (`ops/tools/human_tasks.py` が未存在で即死)。
- [ ] `grep -rqiE 'human.?tasks|鍵作業' apps/ops-dashboard/app/src/`
  — Mission Control 側に鍵作業キューの実装 (lib・描画) が存在すること。
  実測 rc=1 (src/ 配下に一致ゼロ。Next.js 化で参照が消えたことの再確認でもある)。
- [ ] `python3 -m unittest ops.tests.test_human_tasks -v`
  — parse の unittest が green であること (取り消し線除外・非 bullet 行の排除を含む)。
  実測 rc=1 (モジュール未存在)。

## 設計方針

### 前提 (initializer が 2026-08-24 にコード読解・実測で確認)

- **抽出源**: `ops/projects/seeds.md` の 57 行目、見出し
  `## 人間の鍵作業として残るもの (プロジェクトにせず briefing で見せる)`。節内は
  現在 bullet `- T-NNNN: ...` 形式の 4 件のみ。ただし**同一節内に旧リスト構造の名残である
  番号付き行 (14.〜21.) が混在しており、item 18 には取り消し線 `~~...~~` がある**。
  抽出条件を「節内かつ行頭が `- ` かつ `T-\d+:` に一致、`~~` を含む行は除外」にすれば
  この混在に自然に耐える (番号付き行は行頭が数字なので最初から弾ける)。節ベース+行パターン
  なので項目の増減に自動追従する
- **age_days の源泉**: 4 id はいずれも `ops/backlog.json` に `status: needs-human` で存在し、
  `"created": "2026-08-06"` を持つ (id 突合で実測済み)。backlog.json の created を join して
  日数を出すのが素直。欠落時の扱い (何日とするか) は worker が決めてテストで固定すること
- **dashboard の runtime に python3 は無い**: イメージは node:22-alpine + git のみ
  (`apps/ops-dashboard/app/Dockerfile`)。よって dashboard 側は python ツールを exec できず、
  **parse は Python と TypeScript の両方に純関数として実装するしかない**。
  drift 防止のため同じ fixture (seeds 断片) を共有し、両側のテストで同入力→同出力を固定する
  (TS 側テスト基盤は既存: `app/tests/*.test.ts`, `npm test` = tsx --test)
- **seeds.md の取得経路**: dashboard は既存の `lib/ops-state.ts` `loadFromGit()` と同じ型で、
  CACHE_DIR (/tmp/mission-control-state) の shallow fetch 済み repo から
  `git show origin/main:ops/projects/seeds.md` で取れる (`archive.jsonl` と同型)。
  LOCAL_DIR 上書きパターンも踏襲する
- **描画の置き場所**: `page.tsx` は live/projects/attention の 3 view。
  『あなたの手が要ること』は attention (要対応キュー) と並ぶ/その中の独立節が自然。
  既存の veto/stalled/question とは種別が違う (器の出す状態ではなく人間への依頼) ため、
  AttentionItem の kind 汚染より別セクションの方が混線しない
- **完了報告の口**: 新規 API は作らない。既存 `api/feedback/route.ts` (POST /api/feedback、
  kind task-request 許可) と `FeedbackForm.tsx` への案内リンクを各項目に添えるだけ

### 作り方

1. `ops/tools/human_tasks.py`: 標準ライブラリのみ (autopilot イメージの py 方針と同じ)。
   parse を引数渡しの純関数に切り出し、CLI (--out) は薄く。JSON は `{"tasks": [...]}` 形式、
   古い順 (age_days 降順) を想定 — verify は順序を問わないが画面要件が「古い順」なので
   ツール側でも揃えておく
2. dashboard: `lib/human-tasks.ts` (取得 + parse 純関数) → snapshot か page の server 側で
   呼び、attention view に『あなたの手が要ること』節を追加。古い順表示、完了報告は
   feedback フォームへの案内リンクのみ
3. テスト: Python 側 `ops/tests/test_human_tasks.py` (fixture に取り消し線・番号付き混在を含める)、
   TS 側 `app/tests/` に同 fixture で mirror テスト

## やらないこと

- **deployment.yaml (イメージ digest 反映) には触らない** (spec DoD (4))。digest 更新は
  別 PR の手順 (build-dashboard-image.yml → 実測 → 反映)
- **完了報告用の新規 API・新規 state 管理** — 既存 feedback POST への案内で足りる。
  「解消済みの管理」は seeds.md の編集 (取り消し線/削除) が単一の情報源であり続ける
- **backlog.json / needs-human 状態の書き換えや同期** — 抽出は読み取り専用。
  backlog 側との二重管理を作らない (created の join まで。逆方向の sync はしない)
- **seeds.md 自体の整理 (番号付き行の残骸除去)** — 別論点。本プロジェクトは現状の形に耐える
  パーサを作る側で対処する
- **Discord (push 型) への鍵作業通知** — 今回の論点は pull 型ダッシュボードの面の復活のみ。
  briefing への人間の鍵作業表示は既存経路があり触らない
- **extract 対象の拡大 (他節・他ファイル)** — 『人間の鍵作業』節のみ

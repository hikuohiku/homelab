# P-0192 — 朝の挨拶に返事が来たら、秘書は初めて会話になる — Telegram で「次に作ってほしいもの上位 3 つ」を聞き、返信を seed に変換して次回立案の原料を人間本人から調達する

## 目的

seeds H6 は「人間が欲しいものを聞くのが最良の原料」と言い続けて 3 週間になるが、実際に聞いたことは一度もない。今は往復の部品が揃っている — 送る口 (morning brief P-0174 の送信専用経路)、受ける口 (telegram-adapter が DM を ops-feedback inbox へ流す)、昇格先 (seeds.md を立案原料として読む curriculum)。足りないのは**最初の問いかけ 1 通と、返答を seed に昇格させる配線**だけ。VISION 段階 2 の完了判定は「人間が満足を表明したとき」であり、満足の定義を推測し続けるより本人に聞くほうが安い。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-23、`project/p-0192` の checkout (= main 903b3a58f 時点) で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/projects/logs/P-0192/ask-evidence.json`
  — 問いかけの送信証跡ファイルが存在すること。
  実測 rc=1 (`ops/projects/logs/P-0192/` 自体が未存在)。
- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0192/ask-evidence.json')); assert d.get('message_id') and d.get('sent_at')"`
  — 証跡が「いつ・どのメッセージとして送られたか」を機械可読に持つこと (message_id・sent_at が空でない)。
  実測 rc=1 (FileNotFoundError — 上記と同じくファイル未存在)。
- [ ] `grep -q '人間の要望' ops/projects/seeds.md`
  — 取り込んだ返答 (または沈黙の記録) が seeds の新節「人間の要望 (2026-08 募集より)」に
  起こされていること。
  実測 rc=1 (seeds.md に該当節なし。H6 に「聞くのが最良」と書いてあるだけ)。
- [ ] `python3 -m unittest ops.tests.test_wish_seeds`
  — 返答→seed 変換 (と受信側の取り込み契約) が unittest で固定されていること。
  実測 FAILED (errors=1) — ImportError (`ops.tests.test_wish_seeds` 未存在)。

**verify は DoD の下限であって DoD そのものではない。** verify が直接見ないもの —
(1) 送信が本当に **1 通きり**であること (再実行・再走しても二重送信しない)、
(2) 返信ゼロだった場合に「聞いたこと・返ってこなかったこと」が seeds.md に記録されて完成すること
(沈黙も観測。verify 3 は文字列の有無しか見ない)、
(3) ops-feedback inbox の原本を読み取り専用で扱ったこと (CHARTER §5) —
は機械検査不能なので、worker が PROGRESS.md に証跡とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読・実測。調べ直さなくてよい)

- **送信側**: morning brief P-0174 の送信専用経路の実体は `send_telegram()`
  (Telegram Bot API `sendMessage` の 1 通送信。受信系 API は一切触らない) +
  `chat_id = TELEGRAM_ALLOWED_USER_ID` (bot との 1:1 チャットに固定。宛先を引数に
  取れない構造自体が安全性の根拠)。**この実装は本 checkout (main 由来) にまだ無い** —
  P-0174 は `origin/project/p-0174` にあり main 未 merge (2026-08-23 実測。
  spec why の「実装済み」はそのブランチ上の話)。worker は着手時に main 到着を確認し、
  到着していればそれを参照、未着なら同じ型を自前モジュールに最小限写す。
  merge を待って止まらない (ループ優先)
- **credential**: autopilot ns の Secret `telegram-adapter-credentials`
  (`apps/telegram-adapter/external-secret.yaml`、Doppler `homelab/prd` 由来) に
  TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID / AUTOPILOT_GITHUB_TOKEN が揃っている
  (旧 openclaw-credentials の後継。新規 credential は不要)。
  対象キーは `ops/rules.json` の allowed_autopilot_doppler_keys 登録済み
- **in-cluster から api.telegram.org への egress は実証済み** (telegram-adapter Deployment が
  getUpdates を常時 long poll して稼働中)
- **受信側**: telegram-adapter は allowlist の private DM を ops-feedback ブランチの
  `ops/feedback/inbox/<id>.json` へ保存する。note の形は
  `{id, source: "telegram", received, body}` で **kind フィールドを持たない**
  (実物サンプル: origin/ops-feedback の `20260823-120317-1e88e232.json`)。
  `collect_feedback` (`ops/heart/facts.py:183`) は body を `triage.classify` に掛け、
  kind が無くても veto 系キーワードに一致しなければ **review_needed (通常の feedback)
  に落ちる**。つまり対応付けの追加は恐らく不要で、dry-run で確認してから手を入れる
  (spec DoD (2) の指示どおり。「落ちるなら最小限」が条件)
- **昇格先**: `ops/projects/seeds.md` は冒頭に明記のあるとおり curriculum-generate が
  立案原料として読む。新節は H6 (この募集の元ネタ) を持つ主食節の近くに置くのが自然
- unittest の流儀は `ops/tests/test_*.py` + `python3 -m unittest`。
  純関数 + fixture の合成入力で両方向固定するのが check スクリプト群の定石
  (P-0071/P-0105/P-0174 と同じ)

### 作り方

1. **問いかけの送信** — 本文は spec 固定文言「生活で面倒に感じていることを上位 3 つ教えてください
   (homelab で自動化できそうなもの)」。**1 通きり・定期化しない**ので CronJob ではなく
   1 回限りの Job (または session 内での直接実行) にする。GitOps の経路
   (Git → CI → ArgoCD) で実行物を置くなら、Job 再適用に備えて最初から
   `argocd.argoproj.io/sync-options: Force=true,Replace=true` を付ける (substrate の定石)。
   送信文の組み立ては純関数に分け、テストから触れる形にする
2. **証跡** — 送信成功時に Telegram 応答の `result.message_id` と送信時刻 (ISO 8601) を
   `ops/projects/logs/P-0192/ask-evidence.json` へ書く。in-cluster Job からは
   Contents API (AUTOPILOT_GITHUB_TOKEN) で本プロジェクトのブランチへ置けば
   人間の手を介さず repo に乗る。session 内から直接送る場合も同じ形式で残す。
   message_id があるので「送った事実」は自己申告ではなく Telegram 応答の実測である
3. **受信の確認** — `collect_feedback` が telegram 由来の note を review_needed として
   数えられることを fixture で dry-run 実証する (ops-feedback ブランチ本体は読むだけで
   書かない・壊さない)。kind 扱いで落ちると実証された場合のみ最小限の対応付けを入れ、
   それをテストで固定する
4. **seed 化** — 返答本文を `ops/projects/seeds.md` の新節
   「## 人間の要望 (2026-08 募集より)」に 1 行 1 要望で起こす (最低 1 件)。
   返信ゼロでも「聞いたこと・返ってこなかったこと」を記録して完成。
   変換ロジックは `ops/tests/test_wish_seeds.py` で「返信あり」「ゼロ件 (沈黙)」
   「veto 語が本文に混じる通常文」の 3 系列を固定する

## やらないこと

- **返信への自動返信・会話の続行**。問いかけは 1 通きり。返事の行き先は seed 化のみ
- **取り込んだ要望の即時実装・curriculum への直接注入**。seed は次回以降の立案原料。
  「聞いたから作る」は 1 PR 1 論点 (CHARTER) に反する
- **telegram-adapter 本体 (Go) / autopilot-core / morning brief 実装の改修**。既存の
  受信経路と MCP 送信ツールは触らない (dry-run で不足が実証されたときの最小対応付けは除く)
- **Discord 経路での募集併用・複数チャネル展開**。spec は Telegram 1 通に限定
- **定期的な募集への発展 (CronJob 化)**。「予算規則: 追加送信なし」により今回は 1 回限り。
  続ける価値が観測されたら別プロジェクトで提案する
- **ops-feedback inbox の書き換え・削除** (CHARTER §5 触ってはいけないもの)

<!--
heart-and-projects の initializer プロンプト。runner.py が最初のセッションで使う。
プレースホルダ {{...}} は runner.py が埋める。
-->

あなたはプロジェクト {{PROJECT_ID}} の initializer。このリポジトリの checkout はブランチ
{{PROJECT_BRANCH}} にいる。あなたの仕事は **{{PROJECT_FILE}} を作って commit する。それだけ。**
実装を始めてはいけない。

## 採択された仕様 (main に固定済み。これがすべての根拠)

```json
{{SPEC_JSON}}
```

## {{PROJECT_FILE}} に書くこと

1. **目的** — この spec の why を 2〜3 行で
2. **受入チェックリスト** — spec の `verify` コマンド列を 1 項目ずつ `- [ ]` で列挙し、
   各項目に「何を確認しているか」を 1 行添える。**全項目が現時点で failing であること**
   (wrapper が実測で確認済み。もし通っているものがあれば、それは仕様の誤りなので
   {{PROJECT_FILE}} に書かずに終了して報告すること)
3. **設計方針** — どう作るか。3〜10 行。調べて分かった前提 (関連ファイル・既存パターン) を含める
4. **やらないこと** — スコープ外を明記 (1 PR 1 論点、CHARTER の流儀)

## 守ること

- リポジトリの CLAUDE.md / ops/VISION.md / ops/memory/ を読んでから書く
- {{PROJECT_FILE}} と {{PROGRESS_FILE}} (空でよい、見出しだけ) を作り、
  `git add {{PROJECT_FILE}} {{PROGRESS_FILE}} && git commit` する。push は wrapper がやる
- 後続の worker セッションは毎回フレッシュに起動され、**{{PROJECT_FILE}} と
  {{PROGRESS_FILE}} と git log だけを文脈として引き継ぐ**。
  そこに書かなかったことは存在しなかったことになる

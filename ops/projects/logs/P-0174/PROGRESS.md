# P-0174 PROGRESS

後続セッションは PROJECT.md とこのファイルと git log だけを文脈として引き継ぐ。
やったことをここに残す。ここに書かなかったことは存在しなかったことになる。

## 2026-08-23 initializer

- PROJECT.md / PROGRESS.md を作成。verify 2 項目とも failing を実測
  (`test -f apps/openclaw/morning-brief-cronjob.yaml` → rc=1、
  `python3 -m unittest ops.tests.test_morning_brief` → `FAILED (errors=1)` モジュール不在)。
- 調査で確定した前提を PROJECT.md の「前提」節に記録。要点:
  - 情報源は ops-health-report ブランチ (latest.json + history/YYYY-MM-DD.jsonl) と
    origin/ops-state:projects.json のみで新規データ不要
  - **projects.json に納品時刻フィールドが無い** — 「前日の delivered」の数え方
    (main の merge commit 日時 or GitHub API merged_at) が worker の最初の論点
  - P-0161 の分離プロファイル (ops/profiles/private-data/) は本ブランチに未着。
    本プロジェクトは私的データを読まないので待たない

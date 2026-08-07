# ops/projects/ — プロジェクトの恒久記録

## archive.jsonl

curriculum が立てた **全案** (採択・棄却を問わず) の追記専用台帳。1 行 1 JSON。

- **追記のみ。** 過去行の書き換え・削除は禁止 (`ops/validate.py` が origin/main との
  先頭一致で検査する)。採択 spec を後から直したいときは同じ id の行を追記する
  (runner は同 id の最後の行を読む)
- 棄却案・失敗したプロジェクトも消さない。curriculum-generate が「既出と同型の案を
  出さない」ための条件づけに全量を使う (剪定すると同じ案が再提案され続ける)
- 書き手は curriculum Job の PR と、heart の日次スナップショット PR (結果の反映) のみ

## 行のスキーマ

```json
{"id": "P-0001", "title": "...", "why": "...", "cell": ["領域", "種類"],
 "dod": "...", "verify": ["bash コマンド", "..."], "irreversible": false,
 "capabilities": [], "touches_apps": false,
 "budget": {"soft_cap_tokens": 3000000}, "confidence": "confident",
 "adopted": true, "proposed_at": "2026-08-07T12:00:00Z"}
```

採択案は heart が merge 後に ops-state ブランチの projects.json へ登録し、
予告 (Discord) を経て runner Job になる。ライフサイクルの実行状態は
projects.json 側にあり、ここには残らない (結果は heart が `result` フィールド付きの
行として追記する)。

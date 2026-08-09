# seeds — 器の自己育成プロジェクトの候補プール

heart-and-projects の設計時 (2026-08-07) に洗い出した「器が自分で実装すべきもの」と、
旧 backlog (`ops/backlog.json`、凍結済み) から移送した未完タスクの要約。
**curriculum-generate はここを立案の原料として読む** (丸写しではなく、現状に照らして
価値が残っているかを判断してから案にする)。実装済みになった項目は消してよい。

## 設計時に仕込んだ種

1. ~~check_app_list_sync / check_autopilot_image_pin の CI 配線~~ — token に workflow scope が
   付いたので実装可能。**注意: `.github/` は人間レビュー必須パス** (merge は人間を待つ)
2. ダッシュボードのプロジェクトビュー (ops-state の projects.json / archive.jsonl を可視化) — **P-0001 として採択済み**
3. reviewer の deploy-preview (`just preview`) 配線 — apps/ 変更の検品深度を上げる
4. スキルライブラリ `ops/skills/` — 成功手順を検証済みスクリプト+用途説明で蓄積し、立案・実行時に参照
5. 自己表象 SELF.md — transcript 集計から得意・苦手・失敗パターンを定期生成し curriculum の入力にする
6. trust 窓の自動昇降格 (連続 N 回拒否権不発動 → 窓短縮。veto 疎通実績は 2026-08-07 に確認済み)
7. transcript の構造化 (critic の精読精度向上)
8. Discord bot 双方向化 (返信での veto・フィードバック)
9. claude-code の npm pin + inventory 監視 (現状 pin 無しで latest が焼き込まれる)
10. 通知 digest / daily briefing の実装 (outbox の digest 分を回収する正規経路)
11. 生活ドメイン用 lethal trifecta 分離プロファイル (私的データを読む Job に外部送信経路を持たせない) — **段階 3 の前提**
12. ops-state ブランチの履歴間引き (1 日 ~320 コミット蓄積する)
13. ~~ops-health-reporter の heartbeat 監視を heart 対応にする~~ — **P-0011 として実行中** (旧 loop の app=autopilot 前提を
    app=autopilot-heart に。現状 reporter は「pod が見つからない」を報告し続けている)

## 旧 backlog からの移送 (needs-human / blocked だったもの)

権限開放 (kubectl write・pods/log・workflow scope) で自力実装が可能になったものが多い。
詳細は `ops/backlog.json` の該当エントリを読むこと。

- T-0112: immich-postgres 16.14-1.1.1 の CrashLoopBackOff の実ログ調査 — **pods/log が読めるようになったので自力で可能** (writer capability 宣言でプロジェクト化)
- T-0029: immich postgres / vchord のメジャー更新 (T-0112 の原因判明が前提。irreversible)
- T-0074: terraform plan の in-cluster 定期実行 (T-0107 が前提)
- T-0084: health history のサイズ確認 (軽量、chore 向き)
- T-0116/T-0117: coder workspace home backup の復元試験 → PBS 退役
- T-0118: helm v4 系移行 (azure/setup-helm の v4 対応待ち、blocked のまま)
- T-0120: backup CronJob の append-only 鍵への切り替え — **鍵は 2026-08-07 に登録済み。着手可能**。restic の lock 削除が append-only 鍵で失敗する既知の癖の実機検証込み
- T-0147: release-image.yml 次回実行時の cachix hit/miss 確認 (blocked: 人間の手動実行待ち)

## 人間の鍵作業として残るもの (プロジェクトにせず briefing で見せる)

- T-0107: Proxmox pveproxy 証明書の SAN 不一致 (PVE コンソール作業)。**解消まで terraform apply 禁止**
- T-0140: 旧 LXC 101 (syncthing) の cert/config の物理的な取り出し
- T-0141: .envrc の Tailscale credential 重複調査 (対話セッションでの確認が要る)
- T-0148: ops-dashboard への tailnet 実到達確認 (人間のブラウザ)
14. **利用者レンズの定期検分 (critic-user) の常設** — 旧体制の「レビュー役・利用者視点」の後継。
    人間が見る面 (ダッシュボード・Discord 通知・依頼文) を定期的に実際に見て、嘘・古さ・読む負担を
    指摘する critic の変種。2026-08-08 の「ダッシュボードが旧世界のまま」は人間が見つけた —
    このレンズが常設されていれば器が先に気づけた種類の問題 (移行時の空席、優先度高)
15. **利用上限を停滞と区別する** — 2026-08-08 13:10、アカウントのセッション上限で runner の claude が
    3 連続即死し P-0023 が stalled になった (対話セッションと器が同一サブスクリプションを共有する構造要因)。
    runner が上限系エラーを識別して「リセット時刻まで待機して再開」に落とす + セッション stderr の要約を
    result.json に残す (現状 DEVNULL で診断不能)。恒久策として器専用 credential の分離も検討
18. **【最優先・人間の鍵作業】器のアイデンティティ分離** — 2026-08-09、ハーネス保護
    (CODEOWNERS + ruleset) が初日から無効だったと判明。器が人間本人の PAT で行動しているため
    「hikuohiku の PR を hikuohiku が merge」となり、作者≠レビュアーが構造的に成立しない
    (#416 と #422 はレビュー無しで器が自己 merge していた)。修正は器専用の machine user
    アカウント + PAT への差し替え (人間の作業)。完了までは、保護パスを触るプロジェクトの
    成果 PR は事実上ノーチェックで merge されることを前提に運用する


# P-0145 — #49 の前科がある場所から凍結を解かす — vaultwarden / coder の追従滞留を 1 回の sweep で解消し、inventory に最終確認日を刻む (P-0108 の改題)

## 目的

vaultwarden (1.37.1-alpine、上流に 1.37.2) と coder (v2.35.3、上流に v2.35.4 stable) が
policy=auto のまま滞留し、auto 追従の空白は 17 日以上続いている (#49 は vaultwarden 放置で
クライアント同期が全停止した前科)。今後の見張りは version watcher (P-0126, 稼働中) が担うので、
本案は「見張り」ではなく**今の滞留の解消と台帳の起算日 (last_swept_at)** に役割を絞る。
drift を先に潰して last_swept_at を刻むことで、watcher の警報が最初から意味を持つようにする。
homelab 本体への直接差分 (VISION 段階 2 の主食)。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0145` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -s ops/projects/logs/P-0145/release_notes_vaultwarden.md && test -s ops/projects/logs/P-0145/release_notes_coder.md`
  — 現在版から目標版までのリリースノート原文の証跡が両コンポーネント分、空でなく存在すること。
  実測 rc=1 (`ops/projects/logs/P-0145/` ごと未存在)。
- [ ] `python3 -c "import json; d=json.load(open('ops/inventory.json')); t={x['id']:x for x in d['targets']}; assert t['vaultwarden'].get('last_swept_at') and t['coder'].get('last_swept_at')"`
  — inventory.json の vaultwarden / coder 両 target に last_swept_at (ISO8601) が刻まれていること。
  実測 rc=1 (AssertionError — キー未存在)。
- [ ] `test -s ops/projects/logs/P-0145/sweep.md && grep -qE 'vaultwarden|coder' ops/projects/logs/P-0145/sweep.md`
  — 棚卸し記録 (対象・現状・判定・PR 番号) が存在し、対象 2 件に言及していること。
  実測 rc=1 (未存在)。

verify は DoD の下限であって DoD そのものではない。DoD の「ArgoCD sync 後に両アプリが
Healthy であることを health ブランチで確認」は verify が見張らない — PROGRESS.md に
health ブランチの `generated_at` と両アプリの状態を証跡として残すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実測・実読した。調べ直さなくてよい)

- pin の所在は各 1 箇所のみ (apps/ 配下 grep 実測): `vaultwarden/server:1.37.1-alpine` は
  apps/vaultwarden/deployment.yaml:41、`ghcr.io/coder/coder:v2.35.3` は
  apps/coder/deployment.yaml:23。**両 target とも mirrors 未設定** — 同 PR で揃えるべき
  二重管理 pin は無い。
- inventory note の除外規定 (ops/inventory.json): vaultwarden「1.37.0 は alpine ビルドが壊れていた
  前例あり。リリースノートは必ず全文読む」/ coder「stable チャンネルのみ (mainline の v2.36.0 系は
  避ける)・DB migration 有無の事前確認・更新中に自分の足場が消えないよう注意」。DB migration の
  事前確認手法の前例が T-0023 にある (migrations/file listing diff 000001〜000500 の完全一致で
  「追加なし」と確定したやり方)。
- `ops/validate.py` の check_inventory (L130-148) は固定キーの空チェックのみで未知キーを拒否しない
  (実読済み) → last_swept_at の追加で CI の `ops state validate` は壊れない。
- `ops/check_version_sync.py` は vaultwarden / coder の本体 image を GROUPS で見ていない
  (mirrors 整合専用)。ただし台帳として、bump と同一 PR で inventory の `current` も新版に揃える
  (CHARTER §1「同じ事実が 2 箇所に書かれていない」)。
- merge 後の健全性確認は health ブランチ経由:
  `git fetch origin ops-health-report && git show origin/ops-health-report:ops/health/latest.json`
  (CHARTER §2)。substrate.md の訂正済み注意あり — coder / vaultwarden の Degraded は backup
  CronJob の子 Job 失敗 (B2 download cap 等) 由来のことがあるので、判定は直近 backup Job の
  成敗も見てからにする。

### 作り方

1. **vaultwarden → coder の順で直列** (CHARTER §2 直列処理、1 PR 1 コンポーネント)。
   vaultwarden を先にするのは #49 の当事者だから。
2. 各コンポーネント共通: 上流タグを実確認 (coder は stable のみ。目標版は実行時の上流最新から
   除外規定に当たらないものを選ぶ) → 現在版の次から目標版までリリースノート原文を読み
   `release_notes_*.md` に保存 (要約ではなく、判断材料になった箇所の引用と URL を残す) →
   apps/*/deployment.yaml の tag 更新 + ops/inventory.json の当該 target の `current` /
   `last_swept_at` (ISO8601 UTC) 更新 → PR。
3. ロールバック手順は PR 本文必須 (medium: patch 更新でも auto-merge の条件)。**DB を持つので
   スキーマの扱いを明記する** (CHARTER §4): vaultwarden は SQLite 起動時 migration、coder は
   PostgreSQL — 「コードは revert できてもスキーマは戻らない」ことを書く。migration 有無の
   事前確認結果も PR 本文へ。
4. merge → ArgoCD sync 後、health ブランチ latest.json で当該 Application が Synced/Healthy に
   戻ったことを実測し PROGRESS.md に記す。両 PR 完了後、sweep.md に
   対象・現状・判定・PR 番号を記して完成 (verify3 が通る形)。

## やらないこと

- **vaultwarden / coder 以外の target の追従**。watcher が今後検出する他の drift、policy=manual
  (coder-workspace-image, claude-code-cli 等への着手) は対象外。1 PR 1 論点
- **watcher / check_version_sync.py / health-reporter 側の改修**。last_swept_at を機械消費する
  仕組みを作る話ではない。今回の人間可読な台帳 (sweep.md) まで
- **coder-postgres 等の別 target 更新への波及**。coder の更新中に古さが目についても触らない
- **major 更新・breaking change を含む更新の強行**。リリースノート読解で breaking が判明したら
  その場で判断せず CHARTER §4 の高リスク手順 (戻せる形への落下) に従う
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)

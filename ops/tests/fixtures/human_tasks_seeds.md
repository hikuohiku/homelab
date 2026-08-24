# seeds — 抽出テスト用の断片 (実物の構造を模した fixture)

P-0272 の parse テスト (Python: ops/tests/test_human_tasks.py、
TypeScript: apps/ops-dashboard/app/tests/human-tasks.test.ts) が**この 1 ファイルを共有**する。
drift 防止が目的なので、項目を変えるときは両側の期待値も一緒に直すこと。

実物 (ops/projects/seeds.md) の『人間の鍵作業』節には旧リスト構造の名残である番号付き行
(14.〜21.) が混在しており、そのうちの 1 件は取り消し線済み。この fixture はその混在を
再現している。

## 【主食】homelab 本体の仕事

- T-0074: terraform plan の in-cluster 定期実行 (T-0107 が前提)
- T-0084: health history のサイズ確認 (軽量、chore 向き)

## 人間の鍵作業として残るもの (プロジェクトにせず briefing で見せる)

- T-0107: Proxmox pveproxy 証明書の SAN 不一致 (PVE コンソール作業)。**解消まで terraform apply 禁止**
- T-0140: 旧 LXC 101 (syncthing) の cert/config の物理的な取り出し
14. **利用者レンズの定期検分の常設** — 旧リスト構造の名残。番号付き行は抽出しない。
    続きのインデント行も抽出しない。
15. ~~【最優先・人間の鍵作業】器のアイデンティティ分離~~ — **人間が意図的に却下**。
    番号付き + 取り消し線の二重の除外対象。
- T-0141: .envrc の Tailscale credential 重複調査 (対話セッションでの確認が要る)
- ~~T-9001: 取り消し線付き bullet は解消済みとして除外する~~
- T-0148: ops-dashboard への tailnet 実到達確認 (人間のブラウザ)

## 次の節 (ここから先は抽出しない)

- T-0001: 節の外にある項目
- T-0107: 節の外での再掲。最初の節だけを見るので影響しない

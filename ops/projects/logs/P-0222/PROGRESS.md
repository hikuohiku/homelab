# P-0222 PROGRESS

## セッション 1 (2026-08-23 17:05–17:45Z)

### やったこと

**実装は完了。残りは merge 待ち + health レポート待ちのみ。**

1. diff 実内容の取得と分類 (DoD 1): 3 アプリともドリフトは `ConfigMap <ns>/download-budget`
   1 点のみ。Git `data: {}` ↔ live `data.report.json` (帳簿本体)。原因型は **[ii] 生成フィールド**
   (生成元は download-ledger CronJob 毎時 :25)。処置表 → dispositions.md
2. 処置 (DoD 2): 3 アプリの application.yaml に `ignoreDifferences` 追加。
   **キー単位指定は効かない (下記の罠)。`jsonPointers: ["/data"]` 全体指定が正解** —
   name+namespace+kind で当該 1 オブジェクトにピン留めするので範囲は実質狭いまま
3. データ保護 (DoD 3): live ConfigMap への削除・書き換えは一切なし。
   uid 不変・runs 数不変を検証前後で実測 (coder 8 / syncthing 4 / vaultwarden 4)。
   managedFields の owner は `Python-urllib` (ledger script) のまま
4. ライブ検証: ローカルコミットは ArgoCD から見えないため、justfile preview 相当の
   一時的な live spec 変更で **3 アプリ Synced 化を実測 (17:35:50Z)** → baseline へ復帰済み
   (17:42:51Z, OutOfSync/download-budget のみ)。merge 後は同じ経路で恒久 Synced になる

### 分かったこと (次のセッションへの罠リスト)

- **ArgoCD v3.2.1 でキー単位の ignoreDifferences は機能しない**:
  - `jqPathExpressions: ['.data["report.json"]']` → 無視されず OutOfSync のまま
  - `.data.report\.json` (エスケープドット) → ComparisonError `unexpected token "\\"` で比較全体が Unknown
  - `jsonPointers: ['/data/report.json']` → OutOfSync のまま
  - 正規化器は live からだけ report.json を剥がすため「Git data:{} 残存 vs live data 消滅」の
    非対称が残る。argocd #25157 同型。**`/data` 全体指定なら両側消えて Synced** (17:35Z 実測)
- **root app (`apps`) は子 Application の spec.ignoreDifferences 差分を無視する** (Synced 判定)。
  つまり手動で子に足した ignoreDifferences は root selfHeal では消えない → 手動除去が必要だった。
  (targetRevision patch とは挙動が違う。preview 運用時の知見として有用)
- この環境の罠: argocd CLI 無し / mktemp が壊れている (Permission denied、/tmp/opencode も書けない) /
  jsonpath テンプレート内の文字列連結は不可。refresh 即席化は
  `kubectl annotate application <app> -n argocd argocd.argoproj.io/refresh=normal --overwrite`
- 帳簿が消えない構造的裏付け: report.json の managedFields owner は ledger script
  (manager Python-urllib)。ArgoCD sync は last-applied ベースでも SSA でもこの所有権を奪わない

### 現在地と次の一歩

- コミット: 7de65b2e4 (初版実装) → 64ba4262e (/data 全体指定へ修正 + 実測記録)。
  wrapper push 待ち
- **verify(1) は現時点で failing のまま (= 正しい)**: merge → root selfHeal が子へ適用 →
  Synced 化 (17:35Z 実測どおり) → 次回 health レポート (:00/:30 産出) で latest.json 反映後に green
- verify(2) は dispositions.md 作成済みで green になるはず (要実測)
- PR 本文には **immich に一切触れていないこと** を明記すること (DoD)。immich の同型ドリフトは
  触っていないので P-0092 の作業域は汚していない

### 発見 (スコープ外・curriculum へ)

- unknown_jobs に `download-ledger` と `pvc-usage-reporter` 自身が載っている namespace がある
  (P-0128 の設計どおり黙って 0 扱いにしない挙動)。LEDGER_RULES への追加はキャリブレーション
  (人間専有作業) を伴う論点なのでここでは触らない
- ArgoCD v3.2.1 の ignoreDifferences キー単位指定の無効さは他アプリでも罠になりうる。
  repo 内に既存使用例は無いが、将来使う場合はこの知見 (dispositions.md 参照) を引き継ぐべき

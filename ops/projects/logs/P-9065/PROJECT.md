# P-9065 — Doppler が消えた朝、どの秘密が二度と生まれないかを前もって知っている — ExternalSecret 25 件の復旧不能境界を実測で確定する

## 目的

全 ExternalSecret の唯一の上流である Doppler が消滅する復旧不能事態 (アカウント消滅・新規ノード再構築) のとき、
「どの秘密の値が器の環境から再生成できず Doppler にしか無いか」を 1 枚に固定する。
P-0175 は『Doppler 遮断でも既存 Secret は持ちこたえる』を実証したが、再生成不能な境界の列挙は未着手だった
(P-9030 判決の hint: recovery-plan.md の実在と秘密値非含有の assert を verify に足す)。値は複製せず、
escrow (P-0217) の前段として「どの鍵が消えると二度と生まれないか」を機械分類で確定する。

## 受入チェックリスト

initializer が実測した結果、**4 項目とも現時点で failing**
(2026-08-25、`project/p-9065` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `test -f ops/tools/secret_recoverability.py`
  — 分類ツール本体が存在すること。
  実測 rc=1 (ファイル未存在)。
- [ ] `python3 -m pytest ops/tests/test_secret_recoverability.py -q`
  — fixture 付き unittest が通ること (分類が決定論的で、テストが実在すること)。
  実測 rc=1 (テストファイル未存在)。
- [ ] `test -f ops/health/secret-recoverability.json && python3 -c "import json;d=json.load(open('ops/health/secret-recoverability.json'));assert d['keys'] and all(k.get('recovery_path') for k in d['keys'])"`
  — 分類結果 JSON がコミットされ、全キーが recovery_path を持つこと (分類不能 0 の実体)。
  実測 rc=1 (ops/health/ ディレクトリ自体が未存在)。
- [ ] `test -f docs/recovery-plan.md && ! grep -qE 'BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY' docs/recovery-plan.md`
  — 人間向け recovery path 文書が存在し、秘密値 (PEM 秘密鍵ブロック) を一切含まないこと。
  実測 rc=1 (docs/recovery-plan.md 未存在)。

verify は DoD の下限であって DoD そのものではない。verify が直接見ないもの —
(1) 分類が実クラスタの ExternalSecret (25 件超) を全件読み込んだ実測に基づくこと、
(2) recovery path が「再生成手順」であって「値を書き写す手順」でないこと、
(3) --selftest が決め打ち fixture に対して正・負両方向の結果を返すこと —
は worker が PROGRESS.md に証跡 (コマンドと実測値) とともに残すこと。

## 設計方針

### 前提 (initializer が 2026-08-25 に実読・実測。調べ直さなくてよい)

- **ExternalSecret マニフェストは main の apps/ に無い。** state-out-of-git 移行 (2026-08-25、
  commit 94808cec「書き置きの ops-feedback ブランチ経路を落とす」) で `origin/ops-state` へ移った。
  本 checkout 時点で ops-state の apps/ には **12 ファイル・16 doc** (vaultwarden×3 / immich×3 /
  coder×4 / dex / argocd / autopilot / ops-dashboard / ops-health-reporter / external-secrets)。
  spec の「25 件超」はクラスタ実態の数 (P-0175 時点 21 item) で、repo の apps/ だけからは不足する。
  分類の入力元は「クラスタ実態 (kubectl get externalsecrets -A)」と「ops-state apps/ のマニフェスト」の
  どちらを正とするか worker が決める (後者は人でなく再現性を優先するなら fixture に焼く)
- **allowlist は repo に git 管理されている**: `ops/rules.json` の `allowed_autopilot_doppler_keys`
  (現状 3 件: CLAUDE_CODE_OAUTH_TOKEN / AUTOPILOT_GITHUB_TOKEN / DISCORD_WEBHOOK_URL)。
  `ops/validate.py` の `check_autopilot_secret_allowlist()` が「autopilot namespace へ Secret を
  作る ExternalSecret は allowlist のキーのみ」を CI で強制しており、エージェント環境 (器) に入る
  Doppler キーはここで閉じている。**分類規則の候補**: Doppler キーがこの allowlist に載る →
  『器の環境 (allowlist の鍵で再生成可能)』、載らない → 『値が Doppler にしか無い』。
  この規則は決定論的で verify の「全キーに recovery_path」と両立する (allowlist 外は
  すべて再生成手順付きの Doppler-only に分類できる)
- 先例: `ops/tools/sops_dependency_map.py` (P-0105) が同族 —「鍵の所在が人間の記憶にしか無い」状態を
  機械が再構築できる JSON 地図に置き換え、fail-closed (走査失敗を整合と偽らない)、タイムスタンプ無しの
  コミット済み生成物を出し、`python3 -m unittest ops.tests.test_sops_dependency_map` で固定テストする。
  JSON 出力先が新設ディレクトリ `ops/health/` (現存しない) である点以外はこの流儀にほぼ一致する。
  人間向け復旧文書の先例は `docs/sops-recovery.md` / `docs/doppler-outage-runbook.md` (P-0175 成果)
- 秘密値は一切扱わない。入力は ExternalSecret の remoteRef のキー名 (大文字スネーク) のみで、
  target Secret の値・Doppler API・git 履歴の値には触れない (T-0110「生ログを git 管理ブランチへ
  持ち出さない」の準用)

### 作り方

1. **`ops/tools/secret_recoverability.py`**: ExternalSecret 全件を読み、各 doc の
   `spec.data[].remoteRef.key` (dataFrom はキー名を列挙せず分類不能になるので検出して
   問題扱いする。validate.py の dataFrom 禁止と同じ発想) を集約し、Doppler キーごとに
   上記 allowlist 規則で分類して `ops/health/secret-recoverability.json` へ出力する。
   schema は `sops_dependency_map.py` 同様のバージョン付き (例: keys / unclassifiable / generated_from)。
   `--selftest` フラグで決め打ち fixture を正・負の両方向で分類して rc を返す
2. **`ops/tests/test_secret_recoverability.py`**: fixture (仮想 ExternalSecret YAML) を読み、
   「allowlist 内 → recoverable」「allowlist 外 → doppler_only」「dataFrom → 分類不能として検出」
   「全キーが recovery_path を持つ」等を固定する。JSON 出力をチェックするテストも含める
3. **`docs/recovery-plan.md`**: 分類結果から recovery path を 1 キーずつ列挙する。
   書くのは再生成手順と参照元 (例: VAULTWARDEN_ADMIN_TOKEN → `vaultwarden hash` で再生成し
   Doppler に登録、B2 系 → Backblaze コンソールで再発行、GitHub トークン → GitHub で再発行)。
   秘密値を書かない (verify 4 の assert)。分類不能は 0 にする (dataFrom が実在したら
   その ExternalSecret を特定して指摘する記述を残し、JSON 側の unclassifiable は空で保つ)

安全弁: `irreversible: false`・`touches_apps: false`。新規ファイル (ops/tools/ 1 本、ops/tests/ 1 本、
ops/health/ の JSON、docs/recovery-plan.md) の追加のみで既存動作は変更しない。allowlist 自体の変更は
行わない (分類規則を結果に合わせて曲げない)。

## やらないこと

- **escrow (P-0217) 本体の実装・値の複製・秘密の持ち出し**。本案は境界の確定と文書化まで。
  値の退避は別プロジェクト (1 PR 1 論点)
- **Doppler の冗長化・代替プロバイダ導入・上流の追加**。分類の結果次第の論点は別プロジェクト
- **ops/rules.json の allowlist 変更**。分類を allowlist に合わせるのではなく、allowlist を
  事実として読む。手を入れたくなったら別論点
- **ExternalSecret マニフェスト自体の変更・追加・削除** (touches_apps: false)。
  ops-state からの読み取り専用が原則
- **backlog.json / state.json / journal の編集**。autopilot 直接 push 領域でコンフリクトする (CLAUDE.md)
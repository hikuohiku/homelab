#!/usr/bin/env python3
"""ExternalSecret の復旧不能境界を確定する (P-9065)。

なぜ要るか: 全 ExternalSecret の唯一の上流は Doppler 一社で、P-0175 は『Doppler 遮断でも
既存 Secret は持ちこたえる』を実証したが、復旧不能事態 (アカウント消滅・新規ノード再構築)
で「どの秘密の値が器の環境から再生成できず Doppler にしか無いか」は列挙されたことが
無かった。このツールは参照する Doppler キーごとに

  - recoverable — 器の環境 (ops/rules.json の allowed_autopilot_doppler_keys に載る鍵)
                 で再生成可能
  - doppler_only — 値が Doppler にしか無い (allowlist 外。再生成手順は recovery_path に
                  手順として書く)

を機械分類して ops/health/secret-recoverability.json に出力する。値の複製はしない
(入力は ExternalSecret の remoteRef.key のキー名のみ)。escrow (P-0217) の前段として
「どの鍵が消えると二度と生まれないか」を 1 枚に固定するのが目的。

入力元: apps/ 配下の ExternalSecret manifest の静的走査 (check_credential_map.py と
同じ網)。クラスタ実態 (kubectl) はこのリポジトリの CI / ヘッドレス実行からは触れない
ため、再現可能な入力を優先した (P-9065 PROJECT.md の設計方針、入力元の選択は worker に
委ねられている)。manifest は ops-state 移行で分かれていたが、本チェックアウトの apps/ が
DECLARED_DOPPLER_KEYS (check_credential_map.py) と一致する 26 キーを列挙できることを
実測済み。

分類規則 (決定論的):
  Doppler キーが allowed_autopilot_doppler_keys に載る → recoverable
  載らない → doppler_only
allowlist は事実として読む (分類を結果に合わせて曲げない)。recovery_path は本ファイルの
RECOVERY_PATHS が唯一の編集場所 — 新しいキーを参照し始めたら必ずここに再生成手順を足す
(足さないと fail-closed で落ちる)。

fail-closed: rules.json の allowlist 欠落・ExternalSecret 0 件・YAML 破損・
remoteRef.key 欠損・dataFrom (キーを列挙できない形)・recovery_path 未定義のキーは
すべて problems として rc!=0。「何も見つけられなかった」は整合でなく走査の失敗。

出力はタイムスタンプを持たない (コミットした生成物の diff を変化の無い実行で汚さない)。
sops_dependency_map.py (P-0105) と同じ流儀。

    python3 ops/tools/secret_recoverability.py          # 分類を生成して rc を返す
    python3 ops/tools/secret_recoverability.py --selftest  # fixture テストを回す

固定テスト: ops/tests/test_secret_recoverability.py
(`python3 -m pytest ops/tests/test_secret_recoverability.py -q` / unittest discover 対応)。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = ROOT / "apps"
RULES = ROOT / "ops" / "rules.json"
OUTPUT = ROOT / "ops" / "health" / "secret-recoverability.json"

SCHEMA_VERSION = 1


# recovery_path の唯一の編集場所。キー → 「再生成手順」。秘密値を書かないこと
# (docs/recovery-plan.md の verify が PEM ブロック非含有を検査する)。
# 新規キーを参照し始めたらここに足す。足さないと fail-closed で落ちる。
RECOVERY_PATHS: dict[str, str] = {
    # --- allowlist (recoverable) ---
    "AUTOPILOT_GITHUB_TOKEN": (
        "GitHub → Settings → Developer settings → Personal access tokens で"
        " 再発行 (contents の write を付ける) し、Doppler (homelab/prd) の"
        " AUTOPILOT_GITHUB_TOKEN を更新する。autopilot と ops-dashboard が共有"
    ),
    "CLAUDE_CODE_OAUTH_TOKEN": (
        "Claude Code の再ログイン (OAuth) で再発行し、Doppler の"
        " CLAUDE_CODE_OAUTH_TOKEN を更新する"
    ),
    "DISCORD_WEBHOOK_URL": (
        "Discord サーバー設定 → 連携サービス → 該当 Webhook の URL を再取得し、"
        " Doppler の DISCORD_WEBHOOK_URL を更新する"
    ),
    "OPENCODE_API_KEY": (
        "opencode プロバイダ (LLM の API キー発行元) のダッシュボードで再発行し、"
        " Doppler の OPENCODE_API_KEY を更新する"
    ),
    "NATS_CORE_NKEY_SEED": (
        "`nats nk -gen` で NKey を再生成し、公開鍵側の認証 (apps/nats/config.yaml の"
        " JWT ユーザー定義) を更新してから、Doppler の NATS_CORE_NKEY_SEED を更新する"
    ),
    "NATS_CONSUMER_NKEY_SEED": (
        "`nats nk -gen` で再生成し、公開鍵側の認証を更新してから Doppler の"
        " NATS_CONSUMER_NKEY_SEED を更新する"
    ),
    "NATS_DASHBOARD_NKEY_SEED": (
        "`nats nk -gen` で再生成し、公開鍵側の認証を更新してから Doppler の"
        " NATS_DASHBOARD_NKEY_SEED を更新する"
    ),
    "NATS_PRODUCER_NKEY_SEED": (
        "`nats nk -gen` で再生成し、公開鍵側の認証を更新してから Doppler の"
        " NATS_PRODUCER_NKEY_SEED を更新する"
    ),
    "TELEGRAM_ALLOWED_USER_ID": (
        "Telegram アカウントの数値 ID を確認し (機密性は低い値)、Doppler の"
        " TELEGRAM_ALLOWED_USER_ID を更新する"
    ),
    "TELEGRAM_BOT_TOKEN": (
        "BotFather で /token を発行し、Doppler の TELEGRAM_BOT_TOKEN を更新する"
    ),
    # --- allowlist 外 (doppler_only) ---
    "GITHUB_HEALTH_REPORTER_TOKEN": (
        "GitHub → Settings → Developer settings → Personal access tokens で"
        " 再発行 (repo の read) し、Doppler の GITHUB_HEALTH_REPORTER_TOKEN を更新する"
    ),
    "B2_ACCOUNT_ID": (
        "Backblaze コンソール → Application Keys で再発行し、Doppler の"
        " B2_ACCOUNT_ID を更新する (restic + B2 の削除権限を持つ鍵)"
    ),
    "B2_ACCOUNT_KEY": (
        "Backblaze コンソール → Application Keys で再発行し、Doppler の"
        " B2_ACCOUNT_KEY を更新する (B2_ACCOUNT_ID と同じ Application Key の secret)"
    ),
    "B2_ACCOUNT_ID_APPEND_ONLY": (
        "Backblaze コンソールで append-only (Capabilities に deleteFiles を含めない"
        " listBuckets/listFiles/readFiles/writeFiles) の Application Key を再発行し、"
        " Doppler の B2_ACCOUNT_ID_APPEND_ONLY を更新する"
    ),
    "B2_ACCOUNT_KEY_APPEND_ONLY": (
        "Backblaze コンソールで append-only の Application Key を再発行し、Doppler の"
        " B2_ACCOUNT_KEY_APPEND_ONLY を更新する (B2_ACCOUNT_ID_APPEND_ONLY と同一キー)"
    ),
    "RESTIC_B2_BUCKET": (
        "Backblaze コンソールで該当バケットの名前を確認する (機密性は低い値)。"
        " Doppler の RESTIC_B2_BUCKET を更新する"
    ),
    "RESTIC_PASSWORD": (
        "再生成不能 — restic リポジトリ初期化時に選んだ passphrase で、既存の"
        " restic リポジトリを復号する唯一の値。Doppler 消滅時は escrow (P-0217) か"
        " Doppler のバックアップからの復元が唯一の経路"
    ),
    "CODER_DB_PASSWORD": (
        "再生成すると coder-postgres の実パスワードと不一致になるため、変更は"
        " postgres 側の ALTER USER と ExternalSecret 更新をセットで行う。"
        " 既存値は Doppler の CODER_DB_PASSWORD にしか無い"
    ),
    "CODER_DB_URL": (
        "CODER_DB_PASSWORD を含む postgres 接続 URL の複合値。パスワード再設定時に"
        " 接続 URL を組み立て直し、Doppler の CODER_DB_URL を更新する"
    ),
    "DEX_ARGOCD_CLIENT_SECRET": (
        "ランダム値 (48 文字)。`openssl rand -hex 24` 等で再生成し、dex と argocd の"
        " 両方の設定に同じ値を反映してから Doppler の DEX_ARGOCD_CLIENT_SECRET を更新"
    ),
    "GOOGLE_OAUTH_CLIENT_ID": (
        "Google Cloud Console → APIs & Services → Credentials の OAuth 2.0 Client ID を"
        " 確認・再発行し、Doppler の GOOGLE_OAUTH_CLIENT_ID を更新する"
    ),
    "GOOGLE_OAUTH_CLIENT_SECRET": (
        "Google Cloud Console → APIs & Services → Credentials で再発行し、Doppler の"
        " GOOGLE_OAUTH_CLIENT_SECRET を更新する (GOOGLE_OAUTH_CLIENT_ID と同一クライアント)"
    ),
    "IMMICH_DB_PASSWORD": (
        "再生成すると immich-postgres の実パスワードと不一致になるため、変更は"
        " postgres 側の ALTER USER と ExternalSecret 更新をセットで行う。"
        " 既存値は Doppler の IMMICH_DB_PASSWORD にしか無い"
    ),
    "TAILSCALE_CLIENT_ID": (
        "Tailscale Admin Console → Settings → OAuth Clients で再発行"
        " (devices:core:read スコープ) し、Doppler の TAILSCALE_CLIENT_ID を更新する"
    ),
    "TAILSCALE_CLIENT_SECRET": (
        "Tailscale Admin Console → Settings → OAuth Clients で再発行し、Doppler の"
        " TAILSCALE_CLIENT_SECRET を更新する (TAILSCALE_CLIENT_ID と同一クライアント)"
    ),
    "VAULTWARDEN_ADMIN_TOKEN": (
        "`vaultwarden hash` で新しいハッシュを生成し、vaultwarden の ADMIN_TOKEN 設定"
        " と Doppler の VAULTWARDEN_ADMIN_TOKEN を更新する"
    ),
}

CLASSIFICATION_RULE = (
    "Doppler キーが ops/rules.json の allowed_autopilot_doppler_keys に載る → "
    "recoverable (器の環境で再生成可能)。載らない → doppler_only (値が Doppler にしか無い)。"
    "recovery_path は本ツールの RECOVERY_PATHS が唯一の編集場所"
)


def load_allowlist(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """rules.json から allowed_autopilot_doppler_keys を読む。fail-closed。"""
    problems: list[str] = []
    try:
        data = json.loads((root / "ops" / "rules.json").read_text(encoding="utf-8"))
    except OSError as e:
        return [], [f"ops/rules.json が読めない: {e}"]
    except json.JSONDecodeError as e:
        return [], [f"ops/rules.json が JSON として壊れている: {e}"]
    keys = data.get("allowed_autopilot_doppler_keys")
    if not isinstance(keys, list) or not keys:
        return [], [
            "ops/rules.json に allowed_autopilot_doppler_keys (list) が無いか空。"
            " allowlist が消えると分類の基準が無くなる"
        ]
    bad = [k for k in keys if not isinstance(k, str) or not k]
    if bad:
        problems.append(
            f"ops/rules.json の allowed_autopilot_doppler_keys に非文字列/空が混ざる: {bad}"
        )
    return sorted(set(keys)), problems


def scan_externalsecrets(apps_dir: Path) -> tuple[list[dict], list[str]]:
    """apps/ 配下の ExternalSecret から Doppler キー参照を集める。

    戻り値は (sources, problems)。sources の各要素は
    {path, keys: [キー名...]}。dataFrom を含むファイルは「キーを列挙できない」問題として
    problems に入れ、sources には keys 無しで載せる (分類不能の実体として unclassifiable に使う)。
    """
    sources: list[dict] = []
    problems: list[str] = []
    if not apps_dir.is_dir():
        return sources, [f"{apps_dir} が存在しない (repo ルートから実行しているか)"]
    paths = sorted(
        p for ext in ("*.yaml", "*.yml") for p in apps_dir.rglob(ext)
    )
    for path in paths:
        rel = path.relative_to(apps_dir).as_posix()
        if "/charts/" in f"/{rel}":
            continue
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as e:
            problems.append(f"apps/{rel}: YAML が読めない: {e}")
            continue
        entry = {"path": rel, "keys": []}
        has_externalsecret = False
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "ExternalSecret":
                continue
            has_externalsecret = True
            spec = doc.get("spec") or {}
            if spec.get("dataFrom"):
                entry["keys"] = None
                problems.append(
                    f"apps/{rel}: spec.dataFrom はキーを列挙できないので分類不能。"
                    " spec.data + remoteRef.key の形にするか、"
                    " secret_recoverability.py を拡張すること"
                )
                continue
            for item in spec.get("data") or []:
                rr = item.get("remoteRef") or {}
                key = rr.get("key")
                if not key:
                    problems.append(
                        f"apps/{rel}: data[{item}] に remoteRef.key が無い"
                        " (どの Doppler キーを指すか分からない)"
                    )
                else:
                    entry["keys"].append(key)
        if not has_externalsecret:
            continue
        if entry["keys"] is not None:
            entry["keys"] = sorted(set(entry["keys"]))
        sources.append(entry)
    return sources, problems


def classify_keys(
    keys: set[str],
    allowlist: set[str],
    recovery_paths: dict[str, str],
) -> tuple[list[dict], list[str]]:
    """純関数。キー集合を分類して entries と problems を返す。

    allowlist 内 → recoverable / 外 → doppler_only。recovery_path が無いキーは
    分類に載せず problems に入れる (fail-closed。新しい鍵は必ず手順を足す)。
    """
    problems: list[str] = []
    entries: list[dict] = []
    for key in sorted(keys):
        if key not in recovery_paths:
            problems.append(
                f"Doppler キー '{key}' の recovery_path が RECOVERY_PATHS に未定義。"
                " secret_recoverability.py に再生成手順を足すこと (値は書かない)"
            )
            continue
        classification = (
            "recoverable" if key in allowlist else "doppler_only"
        )
        entries.append(
            {
                "key": key,
                "classification": classification,
                "recovery_path": recovery_paths[key],
            }
        )
    return entries, problems


def build_report(
    root: Path = ROOT,
    apps_dir: Path | None = None,
    rules: Path | None = None,
) -> tuple[dict, list[str]]:
    """走査 → 分類レポート (dict) と problems。main とテストの共通本体。"""
    apps_dir = apps_dir or root / "apps"
    allowlist, problems = load_allowlist(root if rules is None else root)
    sources, scan_problems = scan_externalsecrets(apps_dir)
    problems.extend(scan_problems)

    all_keys: set[str] = set()
    referenced_by: dict[str, list[str]] = {}
    unclassifiable: list[dict] = []
    for src in sources:
        rel = f"apps/{src['path']}"
        if src["keys"] is None:
            unclassifiable.append(
                {"source": rel, "reason": "spec.dataFrom はキーを列挙できない"}
            )
            continue
        for key in src["keys"]:
            all_keys.add(key)
            referenced_by.setdefault(key, []).append(rel)

    if not all_keys:
        problems.append(
            "ExternalSecret から Doppler キーを 1 つも見つけられなかった。"
            " apps/ から ExternalSecret が消えたか、走査の網が壊れている"
        )

    entries, classify_problems = classify_keys(all_keys, set(allowlist), RECOVERY_PATHS)
    problems.extend(classify_problems)
    for entry in entries:
        entry["referenced_by"] = sorted(referenced_by.get(entry["key"], []))

    return (
        {
            "schema_version": SCHEMA_VERSION,
            "generated_from": "apps/ 配下の ExternalSecret manifest の静的走査"
            " (クラスタ到達不能でも再現可能。check_credential_map.py と同じ網)",
            "classification_rule": CLASSIFICATION_RULE,
            "allowlist": sorted(allowlist),
            "keys": entries,
            "unclassifiable": unclassifiable,
            "problems": problems,
        },
        problems,
    )


def run_selftest() -> int:
    """合成 fixture で分類が決定論的に正・負の両方向を返すことを証明する。"""
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(ROOT / "ops" / "tests"),
        pattern="test_secret_recoverability.py",
        top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return run_selftest()

    try:
        report, problems = build_report(ROOT)
    except Exception as e:  # noqa: BLE001 — 走査に失敗したら成功扱いにしない
        print(f"::error::走査に失敗しました: {type(e).__name__}: {e}")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    counts = {"recoverable": 0, "doppler_only": 0}
    for entry in report["keys"]:
        counts[entry["classification"]] += 1
        print(
            f"# {entry['key']} [{entry['classification']}]"
            f" refs={len(entry['referenced_by'])}"
        )
    print(
        f"# {len(report['keys'])} keys:"
        f" recoverable={counts['recoverable']}"
        f" doppler_only={counts['doppler_only']}"
        f" unclassifiable={len(report['unclassifiable'])}"
    )
    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        print(f"::error::復旧不能境界に問題が {len(problems)} 件あります", file=sys.stderr)
        return 1
    print(f"ok: 分類を {OUTPUT} に書きました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
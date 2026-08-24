#!/usr/bin/env python3
"""apps/ 配下の credential 参照の地図と宣言が一致しているか機械検査する (P-0071)。

なぜ要るか: apps/ 配下で ExternalSecret / secretKeyRef による credential 参照が
30 ファイル超に散在する一方、宣言済みの一覧は autopilot 向け
`allowed_autopilot_doppler_keys` (ops/rules.json, 4 キー) しか無かった。他アプリが
credential 参照を増やしても誰も気づかず、「鍵の地図」は人間の記憶にしか存在しない。
棚卸し (T-0065) のように「やった時点」でしか効かない人間の作業を、参照追加のたびに
宣言の更新を強制する CI に置き換える。test_backup_coverage.py (P-0047) と同じ発想。

検査するもの (3 種 + 免除の健全性):

1. ExternalSecret `remoteRef.key` — DECLARED_DOPPLER_KEYS に全て載っていること。
   新しい Doppler キーを参照したらここへ足す (Doppler 側への登録は人間の作業)
2. ExternalSecret `target.name` — DECLARED_SECRET_TARGETS に全て載っていること。
   k8s Secret 名の増減が地図から黙ってずれないため
3. workload (`Deployment` / `CronJob` / `StatefulSet` / `DaemonSet` / `ReplicaSet` /
   `Job` / `Pod`) の `valueFrom.secretKeyRef` と `envFrom.secretRef` — 参照先 Secret を
   apps/ 配下の manifest (ExternalSecret target または静的 Secret) が同じ namespace で
   作っていること。「参照だけあって作り手が無い」= apply した瞬間に Pod が
   CreateContainerConfigError になる構成を、クラスタに届く前に落とす
4. EXEMPT_SECRET_CONSUMERS の各エントリ — 免除された Secret が repo の manifest で
   作られるようになっていたら「免除が不要になった」ので落とす。
   腐った免除は、ここで潰そうとしている穴と同じ形

**既知の死角** (静的スキャンでは映らない。埋められないので伏せずに書き残る):
  - helm chart が values.yaml をレンダリングして初めて現れる参照
    (apps/immich/values.yaml の DB_PASSWORD、apps/dex/values.yaml の envFrom)。
    参照先の Secret 自体は repo 内の ExternalSecret が作っているので実害はないが、
    「values.yaml だけで参照を足す」変更はこの検査に映らない
  - manifest の文字列値に埋め込まれたスクリプト内の参照
    (apps/coder/workspace-home-backup-cronjob.yaml が coder ワークスペース向けに
    生成する pod spec)。同様に参照先は repo 内で宣言済み
  - secretStoreRef を doppler 以外 (例: Vault) に差し替えても、キー名の形が同じなら
    検査は通ってしまう。「どのプロバイダから取るか」までは見ていない
  この 3 つは「テストが通った = 全部守られている」ではないという意味での限界。

判定は走査 (scan_apps) と純関数 (find_violations) に分けてある。実リポジトリだけを
見る検査は「今たまたま通っている」と「正しい」を区別できないので、純関数側は
合成入力で両方向 (落ちること / 通ること) を固定する:
ops/tests/test_check_credential_map.py。単体で確認するには:

    python3 ops/check_credential_map.py            # 実リポジトリの検査 (CI 配線先)
    python3 ops/check_credential_map.py --map      # 地図の列挙だけ表示
    python3 ops/check_credential_map.py --selftest # 合成 fixture で違反を落とす証明

fail-closed: YAML の読み込み失敗・remoteRef.key 等の必須項目欠損・dataFrom のような
列挙できない形・「走査が何も見つけなかった」状態はすべて違反として落とす。
抽出に失敗したまま成功扱いにはしない。
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APPS_DIR = ROOT / "apps"

# ---------------------------------------------------------------------------
# 宣言済みの一覧 (= 鍵の地図)。ここが唯一の編集場所。rules.json は触らない:
# allowed_autopilot_doppler_keys は「autopilot Job への注入を許す鍵」という別の
# 意味論を持つので混ぜない (1 PR 1 論点)。
#
# 初期地図は 2026-08-22 の実走査から生成した。「実態 = 宣言」からの出発なので、
# 今この時点で落ちる項目は無い。以後、参照を足す人は必ずここも足す。
# ---------------------------------------------------------------------------

# apps/ 配下の ExternalSecret が remoteRef.key で参照する Doppler (homelab/prd) キーの全種。
DECLARED_DOPPLER_KEYS = frozenset(
    {
        # autopilot 自身 (rules.json の allowed_autopilot_doppler_keys と重複するが別の意味論)。
        # AUTOPILOT_GITHUB_TOKEN は ops-dashboard も参照する
        "AUTOPILOT_GITHUB_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "OPENCODE_API_KEY",
        # NATS (apps/nats)。producer / consumer で鍵を分ける (設計 D13)。
        # NKey の seed。サーバは公開鍵しか持たず、これを使うのはクライアント側
        "NATS_CONSUMER_NKEY_SEED",
        # コア専用 (events.heart.> への publish + events.raw.> の subscribe)
        "NATS_CORE_NKEY_SEED",
        # ダッシュボードの書き置き投稿口専用 (events.raw.homelab.dashboard の publish だけ)
        "NATS_DASHBOARD_NKEY_SEED",
        "NATS_PRODUCER_NKEY_SEED",
        # telegram-adapter (apps/telegram-adapter)。OpenClaw の置き換えで
        # OPENCLAW_GATEWAY_TOKEN は不要になった (control plane を持たない)
        "TELEGRAM_ALLOWED_USER_ID",
        "TELEGRAM_BOT_TOKEN",
        # autopilot 周辺の個別トークン
        "GITHUB_HEALTH_REPORTER_TOKEN",  # ops-health-reporter
        # restic + Backblaze B2 の共通 credential 複合 (immich/syncthing/vaultwarden/coder)
        "B2_ACCOUNT_ID",
        "B2_ACCOUNT_ID_APPEND_ONLY",
        "B2_ACCOUNT_KEY",
        "B2_ACCOUNT_KEY_APPEND_ONLY",
        "RESTIC_B2_BUCKET",
        "RESTIC_PASSWORD",
        # アプリ固有
        "CODER_DB_PASSWORD",
        "CODER_DB_URL",
        "DEX_ARGOCD_CLIENT_SECRET",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "IMMICH_DB_PASSWORD",
        "TAILSCALE_CLIENT_ID",
        "TAILSCALE_CLIENT_SECRET",
        "VAULTWARDEN_ADMIN_TOKEN",
    }
)

# apps/ 配下の ExternalSecret が target.name で作る k8s Secret 名の全種。
# 名前空間は見ていない (現状どの名前も app 接頭辞で一意。衝突するようになったら
# (namespace, name) の組に拡張すること)
DECLARED_SECRET_TARGETS = frozenset(
    {
        "adguard-restic-backup-credentials",
        "adguard-restic-credentials",
        "argocd-dex-client-secret",
        "autopilot-credentials",
        "autopilot-projects-restic-backup-credentials",
        "autopilot-projects-restic-credentials",
        "coder-db-url",
        "coder-postgres-credentials",
        "coder-restic-backup-credentials",
        "coder-restic-credentials",
        "dex-google-oauth",
        "github-health-reporter-token",
        "immich-postgres-credentials",
        "immich-restic-backup-credentials",
        "immich-restic-credentials",
        "operator-oauth",
        "ops-dashboard-github-token",
        "ops-dashboard-nats-credentials",
        "autopilot-core-credentials",
        "nats-credentials",
        "syncthing-restic-backup-credentials",
        "telegram-adapter-credentials",
        "syncthing-restic-credentials",
        "vaultwarden-admin-token",
        "vaultwarden-restic-backup-credentials",
        "vaultwarden-restic-credentials",
    }
)

# 「workload が secretKeyRef/envFrom で参照しているのに、apps/ のどの manifest も
# その名前の Secret を作っていない」ものの免除表。キーは (namespace, Secret 名)、
# 値は理由。理由の書けない免除はただの見落としの追認なので、理由必須。
# helm chart やコントローラが作る Secret 用。2026-08-22 時点で該当なし (空)。
EXEMPT_SECRET_CONSUMERS: dict[tuple[str | None, str], str] = {}

# secretKeyRef / envFrom を走査する workload 系 kind → pod spec へのパス。
_POD_SPEC_PATHS: dict[str, tuple[str, ...]] = {
    "Pod": ("spec",),
    "Job": ("spec", "template", "spec"),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    # CronJob だけ一段深い。素通りさせると Job/CronJob の env が黙って抜ける
    # (初期測定で CronJob 分を取りこぼした実事故あり)
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}


@dataclass
class CredentialRefs:
    """走査で集めた credential 参照の実態。"""

    doppler_keys: set[str] = field(default_factory=set)
    secret_targets: set[str] = field(default_factory=set)
    # (namespace, name) — apps/ 配下の manifest が作る Secret
    created_secrets: set[tuple[str | None, str]] = field(default_factory=set)
    # (namespace, name) — workload が secretKeyRef/envFrom で参照する Secret
    consumed_secrets: set[tuple[str | None, str]] = field(default_factory=set)


def _docs(path: Path):
    """1 ファイル内の全 YAML ドキュメントを返す。空ドキュメントと非 mapping は捨てる。"""
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict):
            yield doc


def _pod_spec(doc: dict) -> dict | None:
    """kind に応じた pod spec を返す。workload 系で無ければ None。"""
    path = _POD_SPEC_PATHS.get(doc.get("kind"))
    # 未知 kind でここを抜けると path=None→空ループ→doc 自身を返してしまう
    # (ConfigMap 等まで workload 扱いになる)。契約は None。
    if path is None:
        return None
    node: object = doc
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def scan_apps(apps_dir: Path) -> tuple[CredentialRefs, list[str]]:
    """apps/ を静的に走査して実態の参照一覧を作る。

    戻り値は (refs, problems)。problems には YAML の parse 失敗や必須項目の欠損など、
    「列挙を諦めるしかない異常」を file 付きで入れる。呼び出し側は problems が空で
    ない限り成功扱いにしないこと (fail-closed)。
    """
    refs = CredentialRefs()
    problems: list[str] = []
    if not apps_dir.is_dir():
        return refs, [f"{apps_dir} が存在しない (repo ルートから実行しているか)"]
    # .yml も走査対象。*.yaml だけにすると .yml に置かれた ExternalSecret が
    # 黙って列挙から漏れ、未宣言キーが検査を素通しする (実測済みの fail-open)
    paths = sorted(
        p for ext in ("*.yaml", "*.yml") for p in apps_dir.rglob(ext)
    )
    for path in paths:
        rel = path.relative_to(apps_dir).as_posix()
        # ベンダリングした helm chart は対象外 (自前で管理している manifest だけを見る)
        if "/charts/" in f"/{rel}":
            continue
        try:
            docs = list(_docs(path))
        except yaml.YAMLError as e:
            problems.append(f"apps/{rel}: YAML が読めない: {e}")
            continue
        for doc in docs:
            kind = doc.get("kind")
            meta = doc.get("metadata") or {}
            ns = meta.get("namespace")

            if kind == "ExternalSecret":
                spec = doc.get("spec") or {}
                target = spec.get("target") or {}
                tname = target.get("name") or meta.get("name")
                # target.name 省略時は ESO が ExternalSecret と同名の Secret を作る
                if not tname:
                    problems.append(
                        f"apps/{rel}: ExternalSecret に target.name も metadata.name も無い"
                        " (作られる Secret 名が確定しない)"
                    )
                else:
                    refs.secret_targets.add(tname)
                    refs.created_secrets.add((ns, tname))
                if spec.get("dataFrom"):
                    problems.append(
                        f"apps/{rel}: spec.dataFrom はキーを列挙できないので対応外。"
                        " spec.data + remoteRef.key の形にするか、"
                        " check_credential_map.py を拡張すること"
                    )
                for entry in spec.get("data") or []:
                    rr = entry.get("remoteRef") or {}
                    key = rr.get("key")
                    if not key:
                        problems.append(
                            f"apps/{rel}: data[{entry}] に remoteRef.key が無い"
                            " (どの Doppler キーを指すか分からない)"
                        )
                    else:
                        refs.doppler_keys.add(key)

            elif kind == "Secret":
                # 静的に値を置く Secret も「作り手」として数える (値そのものは見ない)
                if meta.get("name"):
                    refs.created_secrets.add((ns, meta["name"]))

            else:
                pod_spec = _pod_spec(doc)
                if pod_spec is None:
                    continue
                for field_name in ("containers", "initContainers", "ephemeralContainers"):
                    for container in pod_spec.get(field_name) or []:
                        for env in container.get("env") or []:
                            skr = (env.get("valueFrom") or {}).get("secretKeyRef")
                            if skr is None:
                                continue
                            sname = skr.get("name")
                            if not sname:
                                problems.append(
                                    f"apps/{rel}: env[{env.get('name')}] の"
                                    " valueFrom.secretKeyRef に name が無い"
                                )
                            else:
                                refs.consumed_secrets.add((ns, sname))
                        for ef in container.get("envFrom") or []:
                            sr = ef.get("secretRef") or {}
                            sname = sr.get("name")
                            if not sname:
                                problems.append(
                                    f"apps/{rel}: envFrom.secretRef に name が無い"
                                )
                            else:
                                refs.consumed_secrets.add((ns, sname))
    return refs, problems


def find_violations(
    refs: CredentialRefs,
    declared_keys: frozenset[str],
    declared_targets: frozenset[str],
    exempt_creators: dict[tuple[str | None, str], str],
) -> list[str]:
    """純関数。実態 (refs) と宣言の差分から、違反の説明文リストを返す。"""
    violations: list[str] = []

    if not refs.doppler_keys and not refs.secret_targets:
        violations.append(
            "走査が ExternalSecret を 1 つも見つけられなかった。"
            "apps/ から ExternalSecret が消えたか、走査が壊れている"
        )

    for key in sorted(refs.doppler_keys - declared_keys):
        violations.append(
            f"Doppler キー '{key}' を apps/ の ExternalSecret が参照しているのに"
            " check_credential_map.py の DECLARED_DOPPLER_KEYS に宣言が無い。"
            "新規 credential 参照は Doppler 側の登録とセットでここに足すこと"
        )
    for key in sorted(declared_keys - refs.doppler_keys):
        violations.append(
            f"DECLARED_DOPPLER_KEYS の '{key}' を参照する ExternalSecret が"
            " apps/ に無い。キーを廃止したなら地図からも消すこと (腐った宣言は"
            "「どこで使われているか分からない鍵」を量産する)"
        )

    for name in sorted(refs.secret_targets - declared_targets):
        violations.append(
            f"k8s Secret '{name}' を apps/ の ExternalSecret が作っているのに"
            " DECLARED_SECRET_TARGETS に宣言が無い。check_credential_map.py に足すこと"
        )
    for name in sorted(declared_targets - refs.secret_targets):
        violations.append(
            f"DECLARED_SECRET_TARGETS の '{name}' を作る ExternalSecret が"
            " apps/ に無い。Secret を廃止したなら地図からも消すこと"
        )

    for (ns, name) in sorted(refs.consumed_secrets - refs.created_secrets, key=repr):
        if (ns, name) in exempt_creators:
            continue
        violations.append(
            f"Secret '{ns}/{name}' を workload が secretKeyRef/envFrom で参照しているのに、"
            " apps/ 配下のどの manifest もこの名前の Secret を作っていない"
            " (apply すると CreateContainerConfigError になる)。"
            " ExternalSecret を足すか、helm chart 等が作るなら"
            " EXEMPT_SECRET_CONSUMERS に理由付きで登録すること"
        )

    for (ns, name), reason in sorted(exempt_creators.items(), key=lambda kv: repr(kv[0])):
        if (ns, name) in refs.created_secrets:
            violations.append(
                f"EXEMPT_SECRET_CONSUMERS の {ns}/{name} は既に apps/ 配下の manifest が"
                f" 作っている。免除は不要になったので削除すること (元の理由: {reason})"
            )

    return violations


def print_map(refs: CredentialRefs, problems: list[str]) -> None:
    """--map 用。現在の地図 (実態) を列挙する。"""
    for problem in problems:
        print(f"problem: {problem}")
    print(f"# Doppler keys ({len(refs.doppler_keys)}):")
    for key in sorted(refs.doppler_keys):
        print(f"#   {key}")
    print(f"# k8s Secrets created by manifests ({len(refs.secret_targets)}):")
    for name in sorted(refs.secret_targets):
        print(f"#   {name}")
    print(f"# consumed via secretKeyRef/envFrom ({len(refs.consumed_secrets)}):")
    for (ns, name) in sorted(refs.consumed_secrets, key=repr):
        creator = "ok" if (ns, name) in refs.created_secrets else "NO CREATOR"
        print(f"#   {ns}/{name} [{creator}]")


def run_selftest() -> int:
    """合成 fixture で「違反を実際に落とすこと」を証明する (PROJECT.md の要求)。

    判定ロジックの固定テストは ops/tests/test_check_credential_map.py に置いてあり、
    CI の unittest discover からも走る。--selftest はそれを単独で回す口。
    """
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(
        str(ROOT / "ops" / "tests"),
        pattern="test_check_credential_map.py",
        top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    argv = sys.argv[1:]
    if "--selftest" in argv:
        return run_selftest()

    try:
        refs, problems = scan_apps(APPS_DIR)
    except Exception as e:  # noqa: BLE001 — 走査に失敗したら成功扱いにしない
        print(f"::error::走査に失敗しました: {type(e).__name__}: {e}")
        return 1

    if "--map" in argv:
        print_map(refs, problems)
        return 1 if problems else 0

    violations = find_violations(
        refs, DECLARED_DOPPLER_KEYS, DECLARED_SECRET_TARGETS, EXEMPT_SECRET_CONSUMERS
    )
    # problems (走査の異常) だけでも rc=1 にする。violations が空だからと
    # 成功扱いにすると、dataFrom・壊れた YAML など fail-closed 対象が
    # ::error:: を出しながら CI を緑で通ってしまう
    if problems or violations:
        for message in [*problems, *violations]:
            print(f"::error::{message}")
        print(
            "::error::credential 参照と宣言 (check_credential_map.py の地図) が"
            f"不一致です: 問題 {len(problems)} 件 / 違反 {len(violations)} 件"
        )
        return 1

    print(
        f"ok: apps/ の credential 参照は宣言と一致しています "
        f"(Doppler keys {len(refs.doppler_keys)} 種 / k8s Secrets "
        f"{len(refs.secret_targets)} 種 / secretKeyRef 参照 {len(refs.consumed_secrets)} 組)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

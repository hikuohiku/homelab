#!/usr/bin/env python3
"""クラスタ全体の外向き通信 (egress) の全数台帳を repo から再生成する (P-0203)。

既定拒否 NetworkPolicy の導入は P-0039 / P-0086 / P-0129 / P-0178 と 4 回立案され
4 回進んでいない。共通の未解決前提は「どの workload がどの外部宛先に本当に届く必要が
あるか」の一覧が存在しないことで、推測のまま既定拒否を敷けば backup (Backblaze B2)、
External Secrets (Doppler)、器自身 (GitHub/Telegram) を断って事故る。
この台帳はその一覧を、推測ではなく repo 内の証跡から機械的に作る。
内向きの全数調査 (P-0095 認証なし応答面) の外向き版。

**静的台帳であり、実クラスタへの通信試験は一切しない。**「manifest とコード上、
どこへ出ていくはずか」だけを答える。実際の到達性検証は次のプロジェクト。

走査対象と抽出するもの:

1. apps/** の YAML / .py / .go / .sh / .ts — https(s):// と oci:// の URL、
   restic の `b2:bucket:path` リポジトリ指定。行コメント (#, //) のみにある URL は
   ドキュメントへの参照なので対象外
2. nix/** (.nix) — ノード側 (node01) の egress。Pod ではないので NetworkPolicy の
   管轄外だが、「クラスタ全体」の帳簿としては載せる
3. ops/rules.json の allowed_autopilot_doppler_keys — credential allowlist。
   鍵名から provider 定数経由で host を確定できるもの (Discord webhook /
   Telegram Bot API / Anthropic API) は台帳に載せる。repo に URL 直書きが無くても
   「この鍵が env に入っている以上その先と通信する」のが実態だから

provider 定数の扱い (manifest に host 名が直書きされない穴): Doppler プロバイダ
(api.doppler.com)、restic の b2 バックエンド、Tailscale の coordination server
(controlplane.tailscale.com) 等は manifest 上に host 名が現れずプロバイダ側の定数と
してのみ存在する。これらは PROVIDER_ROWS で明示的に宣言し、source_evidence に
「直書きが無い」ことを書く。黙って補うのでも載せないのでもなく、補い方を明示する。

fail-closed: 走査が見つけた外部宛先のうち attribution 規則で説明できないものは
エラーにする (新しい URL を足した人は台帳の更新を強制される)。必須 host の欠落・
endpoint 数の下限割れでも落とす。抽出漏れのまま成功扱いにはしない。

使い方:

    python3 ops/security/egress_census.py           # docs/security/ 配下に再生成
    python3 ops/security/egress_census.py --check   # 再生成差分ゼロを確認 (冪等)

stdlib のみ (pyyaml 不使用)。YAML は行ベースの簡易パースでしか読まない
(ops/rules.json _comment: 「CI とサンドボックスを stdlib のみで通す repo 慣習」)。
判定ロジックは純関数に分けてあり、合成 fixture による unittest が
ops/tests/test_egress_census.py にある。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APPS_DIR = ROOT / "apps"
NIX_DIR = ROOT / "nix"
RULES_PATH = ROOT / "ops" / "rules.json"
OUT_DIR = ROOT / "docs" / "security"
OUT_JSON = OUT_DIR / "egress-census.json"
OUT_MD = OUT_DIR / "egress-census.md"

# 走査対象の拡張子。Dockerfile / package-lock.json は意図的に除外:
# イメージビルドと依存解決は GitHub Actions runner 上で行われ、クラスタの Pod でも
# ノードでもない (kubelet pull は node レベルの行として別途載せる)。
SCAN_SUFFIXES = {".yaml", ".yml", ".py", ".go", ".sh", ".ts", ".nix"}
SKIP_PARTS = ("/charts/", "/node_modules/", "/.git/")

URL_RE = re.compile(
    r"(?:https?|oci)://(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>\d+))?"
    r"(?=[/\s\"'`,;)\]}]|$)"
)
# restic の B2 リポジトリ指定。b2:$(ENV_VAR):path と b2:literal-bucket:path の両形。
B2_RE = re.compile(r"\bb2:(?:\$\([A-Za-z_][A-Za-z0-9_]*\)|[A-Za-z0-9._-]+):")
B2_ENDPOINT = "api.backblazeb2.com:443"

# 台帳に最低限載っていなければならない host (spec DoD (1) + 重約束の実名列挙)。
# 欠けるのは走査か attribution が壊ているということなので fail-closed。
MANDATORY_HOSTS = frozenset(
    {
        "api.doppler.com",
        "api.backblazeb2.com",
        "api.github.com",
        "api.telegram.org",
        "accounts.google.com",
        "controlplane.tailscale.com",
        "github.com",
        "discord.com",
        "ghcr.io",
        "docker.io",
    }
)

MIN_ENDPOINTS = 8  # spec DoD (1) の下限


class CensusError(Exception):
    """台帳の構築を続けられない。メッセージはそのまま人間に見せる。"""


# ---------------------------------------------------------------------------
# 抽出レイヤ (純関数)。fixture ベースの unittest で両方向固定する。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """走査が見つけた「外向き通信の兆候」1 個分。"""

    path: str  # ROOT からの相対パス
    line_no: int  # 1 起点の行番号
    kind: str  # "url" | "oci" | "b2" | "schema"
    host: str  # host[:port]。b2 は api.backblazeb2.com:443 に正規化済み
    doc_kind: str = ""  # 含まれる YAML doc の kind (非 YAML は空)
    doc_name: str = ""  # 同 metadata.name
    doc_namespace: str = ""  # 同 metadata.namespace
    raw: str = ""  # マッチした行 (トリム済み)


def split_docs(lines: list[str]) -> list[tuple[int, list[str]]]:
    """YAML ファイルを行ブロックのリストに分割する (`---` 単独行が区切り)。"""
    starts = [0] + [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    docs: list[tuple[int, list[str]]] = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(lines)
        docs.append((s, lines[s:e]))
    return docs


def parse_doc_meta(lines: list[str]) -> dict[str, str]:
    """YAML 1 doc ブロックから kind / metadata.name / metadata.namespace を抜く。

    完全な YAML パーサではない。repo の manifest 慣習
    (kind:, metadata: 直下に name:/namespace: がインデント 2 で並ぶ) のみ対応し、
    それ以外は空文字を返す。読めなかったことはエラーにしない — meta は
    source_evidence の補強にのみ使うため (attribution 規則側が本体)。
    """
    kind = name = ns = ""
    in_meta = False
    for ln in lines:
        m = re.match(r"^kind:\s*(\S+)", ln)
        if m:
            kind = m.group(1)
            continue
        if re.match(r"^metadata:\s*$", ln):
            in_meta = True
            continue
        if in_meta:
            m = re.match(r"^ {2}(name|namespace):\s*(\S+)", ln)
            if m:
                if m.group(1) == "name":
                    name = m.group(2).strip("\"'")
                else:
                    ns = m.group(2).strip("\"'")
            elif re.match(r"^\S", ln):
                in_meta = False  # metadata ブロック終端
    return {"kind": kind, "name": name, "namespace": ns}


def comment_prefix(suffix: str) -> str:
    """拡張子ごとの行コメント開始子。"""
    return "//" if suffix in (".go", ".ts") else "#"


def classify_host(host: str) -> tuple[str, str]:
    """host を分類する。戻り値は (category, reason)。

    category:
      external       — クラスタ外への egress 候補 (台帳の endpoint になる)
      cluster_local  — クラスタ内通信 (kubernetes API, *.svc 等)。NetworkPolicy
                       の既定拒否は egress に対するもので、クラスタ内 Service への
                       通信はこの台帳の管轄ではない
      self_public_url— tailnet 上の自サービス公開 URL (*.ts.net)。ingress 側の
                       識別子であり workload が接続先とするものではない
      schema_reference— $schema 等のメタデータ参照。通信しない
    """
    h = host.lower()
    if (
        h == "localhost"
        or h.startswith("127.")
        or h.endswith(".svc")
        or h.endswith(".svc.cluster.local")
        or h == "kubernetes.default.svc"
    ):
        return ("cluster_local", "クラスタ内アドレス (Service/API server)")
    if h.endswith(".ts.net"):
        return (
            "self_public_url",
            "tailnet 上の自サービス公開 URL。ブラウザやクライアント側の"
            "接続先であり、workload からの egress 先ではない",
        )
    return ("external", "")


def endpoint_of_url(url: str) -> str:
    """URL 文字列を host[:port] に正規化する。scheme は落とす (台帳の key 形)。"""
    m = URL_RE.search(url)
    if not m:
        raise CensusError(f"URL として解釈できない: {url!r}")
    host, port = m.group("host"), m.group("port")
    return f"{host}:{port}" if port else host


def extract_findings_from_lines(
    lines: list[str], suffix: str
) -> list[tuple[int, str, str]]:
    """1 ファイル分の行から (line_no, kind, host) を抽出する純関数。

    行コメントのみの行は無視する (# と //)。インラインコメント後ろの URL までは
    見ない — 実リポジトリでその形の誤検出は確認されておらず、やるなら字句解析が
    要る。見逃しが起きたら attribution 段の fail-closed とは逆方向 (抽出過多) に
    働くので、台帳には「余分な行」が載り、人間のレビューで気づける。
    """
    cm = comment_prefix(suffix)
    out: list[tuple[int, str, str]] = []
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith(cm):
            continue
        if "$schema" in stripped:
            # JSON Schema 参照。通信先ではない (apps/autopilot-core/config.yaml 等)
            for m in URL_RE.finditer(stripped):
                out.append((idx + 1, "schema", f"{m.group('host')}:{m.group('port')}" if m.group("port") else m.group("host")))
            continue
        if B2_RE.search(stripped):
            out.append((idx + 1, "b2", B2_ENDPOINT))
            # b2: 行に別 URL が同居することはないが、URL 抽出は続ける
        for m in URL_RE.finditer(stripped):
            scheme = "oci" if stripped[m.start():].startswith("oci://") else "url"
            host = m.group("host")
            host_port = f"{host}:{m.group('port')}" if m.group("port") else host
            out.append((idx + 1, scheme, host_port))
    return out


def scan_paths(roots: list[Path], base: Path = ROOT) -> list[Finding]:
    """与えられたルート以下を走査し、Finding のリストを返す。

    YAML の場合は doc ブロックごとの kind/name/namespace を Finding に添える。
    """
    findings: list[Finding] = []
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(
                p for p in sorted(root.rglob("*"))
                if p.is_file() and p.suffix in SCAN_SUFFIXES
                and not any(part in p.as_posix() for part in SKIP_PARTS)
            )
    for path in sorted(set(files)):
        rel = path.relative_to(base).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CensusError(f"{rel}: 読み込み失敗: {e}") from e
        lines = text.splitlines()
        is_yaml = path.suffix in (".yaml", ".yml")
        doc_bounds: list[tuple[int, int]] = []
        metas: list[dict[str, str]] = []
        if is_yaml:
            for start, block in split_docs(lines):
                doc_bounds.append((start, start + len(block)))
                metas.append(parse_doc_meta(block))
        for line_no, kind, host in extract_findings_from_lines(lines, path.suffix):
            dk = dn = dns_ = ""
            if is_yaml:
                for (s, e), meta in zip(doc_bounds, metas):
                    if s <= line_no - 1 < e:
                        dk, dn, dns_ = meta["kind"], meta["name"], meta["namespace"]
                        break
            findings.append(
                Finding(
                    path=rel,
                    line_no=line_no,
                    kind=kind,
                    host=host,
                    doc_kind=dk,
                    doc_name=dn,
                    doc_namespace=dns_,
                    raw=lines[line_no - 1].strip(),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# attribution レイヤ。Finding (path+host) → 台帳レコードへの対応表。
#
# 「誰がその host と話すのか」は汎用走査では決定できない (URL はファイルにあるが
# 通信主体は workload)。だから対応表として明示的に持ち、走査が見つけた外部宛先が
# 全てこの表で説明できることを build 時に強制する。新しい URL はここへ足すまで
# --check が落ちる = 台帳更新の強制。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """台帳レコード 1 件分の内容。"""

    workload: str
    namespace: str
    endpoint: str  # host[:port]
    reason: str  # なぜその宛先と通信するか (manifest 上の根拠)
    breakage: str  # この穴が塞がれると壊れるもの
    source_evidence: tuple[str, ...] = ()
    open_at_default_deny: bool = True  # 既定拒否時に開けるべき穴か
    open_note: str = ""  # 開け方 (NetworkPolicy 的な当たり方)
    exception_note: str = ""  # autopilot namespace 対象外にする場合の例外理由文言


AUTOPILOT_EXCEPTION_BASE = (
    "暫定例外: lethal trifecta 分離 (stage3 seeds #11) 未実施のため、"
    "既定拒否 NetworkPolicy を autopilot namespace 対象外とする場合がある。"
    "その場合でも本 endpoint は器自身の生存に必須であり、分離適用時には "
    "workload 別の egress ルールとして復刻すること"
)


def _rule(
    globs: tuple[str, ...],
    hosts: tuple[str, ...],
    *,
    workload: str,
    namespace: str,
    reason: str,
    breakage: str,
    open_at_default_deny: bool = True,
    open_note: str = "",
    use_doc_workload: bool = False,
    exception_note: str = "",
    doc_workload_override: dict[str, tuple[str, str]] | None = None,
) -> list[dict]:
    """attribution 規則の展開ヘルパ。glob × host の全組合せを出す。"""
    return [
        {
            "glob": g,
            "host": h,
            "workload": workload,
            "namespace": namespace,
            "reason": reason,
            "breakage": breakage,
            "open": open_at_default_deny,
            "open_note": open_note,
            "use_doc_workload": use_doc_workload,
            "exception_note": exception_note,
            "doc_workload_override": doc_workload_override or {},
        }
        for g in globs
        for h in hosts
    ]


ARGOCD_HELM_REASON = (
    "ArgoCD repo-server が Helm chart の取得先として使う"
    "(kustomization.yaml の helmCharts.repo)"
)

ATTRIBUTION_RULES: list[dict] = [
    # --- ArgoCD: git fetch -------------------------------------------------
    *_rule(
        ("apps/*/application.yaml", "apps/apps.yaml"),
        ("github.com",),
        workload="argocd-repo-server",
        namespace="argocd",
        reason="Application の repoURL https://github.com/hikuohiku/homelab.git を "
        "argocd-repo-server が git fetch する (App of Apps の全 Application)",
        breakage="全 ArgoCD Application の sync が停止し、クラスタの宣言的運用が止まる",
        open_note="egress TCP 443 → github.com (argocd-repo-server / application-controller)",
    ),
    # --- ArgoCD: helm chart fetch -----------------------------------------
    *_rule(
        ("apps/argocd/kustomization.yaml",),
        ("argoproj.github.io",),
        workload="argocd-repo-server",
        namespace="argocd",
        reason="argo-cd chart 自身の取得元 (Helm inflation)",
        breakage="ArgoCD 自身の再デプロイ・upgrade ができない",
        open_note="egress TCP 443 → argoproj.github.io",
    ),
]

ATTRIBUTION_RULES.extend(
    [
        *_rule(
            ("apps/dex/kustomization.yaml",),
            ("charts.dexidp.io",),
            workload="argocd-repo-server",
            namespace="argocd",
            reason=f"dex chart の取得元。{ARGOCD_HELM_REASON}",
            breakage="dex のデプロイ・upgrade ができない",
            open_note="egress TCP 443 → charts.dexidp.io",
        ),
        *_rule(
            ("apps/external-secrets/kustomization.yaml",),
            ("charts.external-secrets.io",),
            workload="argocd-repo-server",
            namespace="argocd",
            reason=f"external-secrets chart の取得元。{ARGOCD_HELM_REASON}",
            breakage="ESO のデプロイ・upgrade ができない",
            open_note="egress TCP 443 → charts.external-secrets.io",
        ),
        *_rule(
            ("apps/tailscale-operator/kustomization.yaml",),
            ("pkgs.tailscale.com",),
            workload="argocd-repo-server",
            namespace="argocd",
            reason=f"tailscale-operator chart の取得元。{ARGOCD_HELM_REASON}",
            breakage="tailscale-operator のデプロイ・upgrade ができない",
            open_note="egress TCP 443 → pkgs.tailscale.com",
        ),
        *_rule(
            ("apps/immich/kustomization.yaml",),
            ("ghcr.io",),
            workload="argocd-repo-server",
            namespace="argocd",
            reason="oci://ghcr.io/immich-app/immich-charts からの OCI Helm chart 取得",
            breakage="immich のデプロイ・upgrade ができない",
            open_note="egress TCP 443 → ghcr.io (OCI)",
        ),
        # --- dex ------------------------------------------------------------
        *_rule(
            ("apps/dex/values.yaml",),
            ("accounts.google.com",),
            workload="dex",
            namespace="dex",
            reason="Google OIDC connector の issuer "
            "(config.issuers.connectors[].config.issuer)。discovery/JWKS/token を "
            "dex server が上流取得する",
            breakage="Google アカウントでの SSO 全不能 (ArgoCD ログインを含む)",
            open_note="egress TCP 443 → accounts.google.com (dex)",
        ),
        # --- version-watcher --------------------------------------------------
        *_rule(
            ("apps/version-watcher/version_watch.py", "apps/version-watcher/watch.py"),
            (
                "api.github.com",
                "hub.docker.com",
                "registry.npmjs.org",
            ),
            workload="version-watcher",
            namespace="version-watcher",
            reason="inventory 対象の最新版確認 (GitHub releases / Docker Hub tags / npm registry)",
            breakage="バージョン監視が止まり、pin の放置による同期停止事故 (#49 型) の早期発見を失う",
            open_note="egress TCP 443 → api.github.com / hub.docker.com / registry.npmjs.org",
        ),
        # --- ops-health-reporter ---------------------------------------------
        *_rule(
            ("apps/ops-health-reporter/report.py",),
            ("api.github.com",),
            workload="ops-health-reporter",
            namespace="ops-health-reporter",
            reason="K8s/ArgoCD 健全性の GitHub への報告と issue 参照 (GITHUB_HEALTH_REPORTER_TOKEN)",
            breakage="autopilot と人間への健全性報告が途絶える (soak 判定も不能化)",
            open_note="egress TCP 443 → api.github.com",
        ),
        # --- telegram-adapter -------------------------------------------------
        *_rule(
            ("apps/telegram-adapter/app/main.go",),
            ("api.telegram.org", "api.github.com"),
            workload="telegram-adapter",
            namespace="autopilot",
            reason="Telegram Bot API の long polling (TELEGRAM_API 既定値) と、"
            "受信 DM を ops-feedback へ流す際の GitHub API (GITHUB_API 既定値)",
            breakage="人間→autopilot の Telegram 受信窓が閉じる",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → api.telegram.org / api.github.com",
        ),
        # --- autopilot-core ---------------------------------------------------
        *_rule(
            ("apps/autopilot-core/app/main.go",),
            ("api.github.com",),
            workload="autopilot-core",
            namespace="autopilot",
            reason="ops-feedback ブランチの読み取り (GITHUB_API 既定値)",
            breakage="コアがフィードバックを読めなくなる",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → api.github.com",
        ),
        # --- autopilot / autopilot-heart ---------------------------------------
        *_rule(
            ("apps/autopilot/loop.sh",),
            ("github.com",),
            workload="autopilot",
            namespace="autopilot",
            reason="git clone/fetch/push (main への帳簿 push と PR)。"
            "credential.helper に https://github.com を設定",
            breakage="autopilot が repo を読み書きできず自律運用全体が停止する",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → github.com (git over HTTPS)",
        ),
        *_rule(
            ("apps/autopilot/heart-bootstrap.sh",),
            ("github.com",),
            workload="autopilot-heart",
            namespace="autopilot",
            reason="heart 起動時の repo clone/fetch (REPO_URL 既定値)",
            breakage="heart-and-projects の起動ができない",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → github.com (git over HTTPS)",
        ),
        # --- ops-dashboard ------------------------------------------------------
        *_rule(
            ("apps/ops-dashboard/app/src/lib/ops-state.ts",),
            ("github.com",),
            workload="ops-dashboard",
            namespace="autopilot",
            reason="HOMELAB_REPOSITORY 既定値を git remote として feedback 投稿に使う",
            breakage="dashboard から ops-feedback への書き置きができなくなる",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → github.com (git over HTTPS)",
        ),
        *_rule(
            ("apps/ops-dashboard/app/src/app/api/feedback/route.ts",),
            ("api.github.com",),
            workload="ops-dashboard",
            namespace="autopilot",
            reason="feedback 投稿時の GitHub API 呼び出し (GITHUB_API 既定値)",
            breakage="dashboard から ops-feedback への書き置きができなくなる",
            exception_note=AUTOPILOT_EXCEPTION_BASE,
            open_note="egress TCP 443 → api.github.com",
        ),
        # --- ops-dashboard のリンク生成 (通信ではない) ---------------------------
        *_rule(
            ("apps/ops-dashboard/app/src/app/api/feedback/route.ts",),
            ("github.com",),
            workload="ops-dashboard",
            namespace="autopilot",
            reason="ISSUE_URL — フィードバック issue へのリンク文字列の生成のみ。"
            "サーバからの通信先ではなくブラウザが開く先なので、"
            "既定拒否で開ける穴には数えない",
            breakage="壊れない (リンク文字列)。リンク先自体は人間の閲覧経路",
            open_at_default_deny=False,
            exception_note=AUTOPILOT_EXCEPTION_BASE,
        ),
        # --- restic backup (b2:) — doc 名から workload を取る --------------------
        *_rule(
            (
                "apps/*/restic-backup-cronjob.yaml",
                "apps/*/workspace-home-backup-cronjob.yaml",
            ),
            (B2_ENDPOINT,),
            workload="",  # use_doc_workload=True で埋める
            namespace="",
            use_doc_workload=True,
            # coder の workspace-home backup は CronJob 本体ではなく、同梱 ConfigMap
            # (workspace-home-backup-script) 内の埋め込み pod spec が b2: を持つ。
            # スクリプトはその CronJob の Pod 内で走るので、workload 名は
            # ConfigMap でなく消費側 CronJob に寄せる
            doc_workload_override={
                "workspace-home-backup-script": ("coder-workspace-home-backup", "coder"),
            },
            reason='RESTIC_REPOSITORY="b2:$(RESTIC_B2_BUCKET):…" — restic の '
            "Backblaze B2 バックエンドは api.backblazeb2.com:443 と話す "
            "(restic b2 backend の provider 定数。manifest には host 名は出ない)",
            breakage="バックアップと retention (forget --prune) の両方が失敗し、"
            "災害時に復元点が無い。PVC 単独障害でデータロストに直結",
            open_note="egress TCP 443 → api.backblazeb2.com (各 restic CronJob)",
        ),
        # --- nix (node01) -------------------------------------------------------
        *_rule(
            ("nix/images/proxmox-cloud/configuration.nix",),
            ("cache.nixos.org", "hikuohiku.cachix.org"),
            workload="node01/nix-daemon",
            namespace="(node)",
            reason="nix.settings.substituters — ノード構築・再構成時の binary cache 取得",
            breakage="ノードの再構築・設定変更適用ができない (Pod ではないため "
            "NetworkPolicy 管轄外。node firewall レイヤーの統制対象)",
            open_at_default_deny=False,
            open_note="Pod NetworkPolicy の対象外 (node egress)。",
        ),
        *_rule(
            ("nix/images/proxmox-cloud/k3s-manifests.nix",),
            ("argoproj.github.io",),
            workload="node01/helm-controller (bootstrap)",
            namespace="(node)",
            reason="初回ブートストラップの HelmChart CR (argo-cd) の取得元。"
            "ArgoCD 起動後は apps/argocd/kustomization.yaml 側に主導権が移る",
            breakage="空クラスタからの ArgoCD ブートストラップができない",
            open_at_default_deny=False,
            open_note="bootstrap 時のみ。helm-controller は node 側で動くため "
            "Pod NetworkPolicy の対象外",
        ),
        *_rule(
            ("nix/images/proxmox-cloud/k3s-manifests.nix",),
            ("github.com",),
            workload="argocd-repo-server",
            namespace="argocd",
            reason="ブートストラップ用 root Application CR (apps) の repoURL。"
            "ArgoCD 起動後は apps/apps.yaml 側が同じものを管理する",
            breakage="空クラスタからの App of Apps 起点が作れない",
            open_note="egress TCP 443 → github.com (argocd-repo-server)",
        ),
    ]
)


# ---------------------------------------------------------------------------
# provider 定数行。走査では見つからない (manifest に host 直書きが無い) 穴。
# ---------------------------------------------------------------------------


def _allowlisted_keys() -> set[str]:
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise CensusError(f"ops/rules.json を読めない: {e}") from e
    return set(data.get("allowed_autopilot_doppler_keys") or [])


CREDENTIAL_HOST_MAP = {
    # 鍵名 → (endpoint, 根拠, 壊れるもの)
    "DISCORD_WEBHOOK_URL": (
        "discord.com:443",
        "通知 webhook の POST 先 (ops/heart/notify.py の _post_discord)。URL 自体は "
        "Doppler から注入されるため repo に host 直書きは無い",
        "予告・納品・障害の即時通知が全て失われる (digest も届かない)",
    ),
    "TELEGRAM_BOT_TOKEN": (
        "api.telegram.org:443",
        "Telegram Bot API。許可鍵リストに入っている以上、直接送信経路として存在しうる "
        "(主経路は telegram-adapter)",
        "autopilot からの Telegram 通知が失われる可能性がある",
    ),
    "CLAUDE_CODE_OAUTH_TOKEN": (
        "api.anthropic.com:443",
        "Claude Code OAuth の接続先 (provider 定数。repo 内に直書きは無い)",
        "autopilot の思考エンジンが動かず、全プロジェクトが停止する",
    ),
}


def build_provider_rows(
    allowlisted_keys: set[str] | None = None,
) -> list[Row]:
    """provider 定数行の構築。allowlisted_keys を省略すると実リポジトリの
    ops/rules.json を読む (テストでは合成値を注入できる)。"""
    rows: list[Row] = []
    if allowlisted_keys is None:
        allowlisted_keys = _allowlisted_keys()
    for key in sorted(allowlisted_keys):
        if key not in CREDENTIAL_HOST_MAP:
            continue
        endpoint, reason, breakage = CREDENTIAL_HOST_MAP[key]
        rows.append(
            Row(
                workload="autopilot-heart",
                namespace="autopilot",
                endpoint=endpoint,
                reason=f"allowed_autopilot_doppler_keys の {key} 由来。{reason}"
                f" [source_evidence: ops/rules.json allowed_autopilot_doppler_keys]",
                breakage=breakage,
                source_evidence=("ops/rules.json allowed_autopilot_doppler_keys", key),
                open_at_default_deny=True,
                open_note=f"egress TCP → {endpoint} (autopilot-heart)",
                exception_note=(
                    AUTOPILOT_EXCEPTION_BASE
                    + "。本行は credential allowlist 由来であり、鍵を rules.json から"
                    "削除すれば台帳からも消れる"
                ),
            )
        )
    # Doppler プロバイダ (ESO)
    rows.append(
        Row(
            workload="external-secrets",
            namespace="external-secrets",
            endpoint="api.doppler.com:443",
            reason="ClusterSecretStore doppler (provider: doppler) — ESO が homelab/prd の"
            " secret を同期しに行く先。host は ESO doppler プロバイダの定数で manifest に"
            "直書きされない [source_evidence: apps/external-secrets/cluster-secret-store.yaml]",
            breakage="全 namespace (12 箇所) の ExternalSecret 同期が停止する。"
            "既存 Secret は残るが refresh 失敗が積み重なり、回転後の新 credential が届かない",
            source_evidence=("apps/external-secrets/cluster-secret-store.yaml (provider: doppler)",),
            open_at_default_deny=True,
            open_note="egress TCP 443 → api.doppler.com (external-secrets controller)",
        )
    )
    # Tailscale coordination
    rows.append(
        Row(
            workload="tailscale-operator (+proxy pods)",
            namespace="tailscale",
            endpoint="controlplane.tailscale.com:443",
            reason="Tailscale client の coordination server。host は Tailscale client の"
            "provider 定数で manifest に直書きされない [source_evidence: "
            "apps/tailscale-operator/kustomization.yaml (chart 導入 + apiServerProxyConfig)]",
            breakage="operator/proxy が tailnet から離脱し、*.ts.net ingress と "
            "K8s API proxy 認証が全滅する",
            source_evidence=("apps/tailscale-operator/kustomization.yaml",),
            open_at_default_deny=True,
            open_note="egress TCP 443 → controlplane.tailscale.com。DERP relay "
            "(*.derp.tailscale.com, UDP/TCP) も状況により必要 — blind_spots 参照",
        )
    )
    # kubelet image pull (node level)
    for reg, note in (
        ("docker.io", "python/restic/busybox/postgres/vaultwarden/syncthing 等の Docker Hub イメージ"),
        ("ghcr.io", "自前イメージ群 (homelab-*) と vectorchord/coder 等"),
    ):
        rows.append(
            Row(
                workload="node01/kubelet (image pull)",
                namespace="(node)",
                endpoint=f"{reg}:443",
                reason=f"{note}。image pull は kubelet = ノード側 egress で "
                "Pod NetworkPolicy の対象外という重要な注意付きで載せる",
                breakage="新規 Pod の起動・再スケジュールができない (既存コンテナは影響なし)",
                source_evidence=("各 Deployment/CronJob の image: 行",),
                open_at_default_deny=False,
                open_note="Pod NetworkPolicy の対象外 (node egress)。node firewall での統制が必要",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 台帳構築 (純関数)。
# ---------------------------------------------------------------------------


NS_ORDER = [
    "external-secrets",
    "argocd",
    "dex",
    "tailscale",
    "immich",
    "vaultwarden",
    "coder",
    "syncthing",
    "version-watcher",
    "ops-health-reporter",
    "autopilot",
    "(node)",
]


def _ns_sort_key(ns: str) -> tuple[int, str]:
    return (NS_ORDER.index(ns) if ns in NS_ORDER else len(NS_ORDER), ns)


def build_records(
    findings: list[Finding],
    rules: list[dict] | None = None,
    include_provider_rows: bool = True,
) -> tuple[list[Row], list[dict]]:
    """Finding 群を台帳レコードへ変換する。

    戻り値は (rows, excluded)。excluded は cluster_local / self_public_url /
    schema_reference の除外集計。attribution できない外部宛先があったら
    CensusError (fail-closed)。rules を省略した場合は実リポジトリの
    ATTRIBUTION_RULES を使う (テストでは合成規則を注入できる)。
    """
    if rules is None:
        rules = ATTRIBUTION_RULES
    excluded_index: dict[tuple[str, str], dict] = {}
    row_map: dict[tuple[str, str, str], dict] = {}
    unattributed: list[Finding] = []

    def add_excluded(host: str, category: str, reason: str, finding: Finding) -> None:
        key = (host, category)
        entry = excluded_index.setdefault(
            key, {"host": host, "category": category, "reason": reason, "evidence": []}
        )
        ev = f"{finding.path}:{finding.line_no}"
        if ev not in entry["evidence"]:
            entry["evidence"].append(ev)

    for f in findings:
        bare_host = f.host.split(":")[0]
        if f.kind == "schema":
            # $schema 参照は host が外部でも通信しない。分類より先に除外
            add_excluded(
                f.host,
                "schema_reference",
                "$schema 等のメタデータ参照。通信しない",
                f,
            )
            continue
        category, why = classify_host(bare_host)
        if category != "external":
            add_excluded(f.host, category, why, f)
            continue

        matched = None
        for rule in rules:
            if not fnmatch.fnmatch(f.path, rule["glob"]):
                continue
            if rule["host"] != f.host and rule["host"] != bare_host:
                continue
            matched = rule
            break
        if matched is None:
            unattributed.append(f)
            continue

        workload = matched["workload"]
        namespace = matched["namespace"]
        if matched.get("use_doc_workload"):
            override = matched.get("doc_workload_override") or {}
            if f.doc_name in override:
                workload, namespace = override[f.doc_name]
            elif not f.doc_name or not f.doc_namespace:
                raise CensusError(
                    f"{f.path}:{f.line_no}: doc 名から workload を確定できない "
                    f"(kind={f.doc_kind!r}, name={f.doc_name!r}, ns={f.doc_namespace!r})"
                )
            else:
                workload = f.doc_name
                namespace = f.doc_namespace
        evidence = [f"{f.path}:{f.line_no}"]
        if f.doc_kind:
            evidence.append(f"(doc: {f.doc_kind} {f.doc_name} @ {f.doc_namespace})")
        key = (namespace, workload, f.host)
        entry = row_map.setdefault(
            key,
            {
                "workload": workload,
                "namespace": namespace,
                "endpoint": f.host,
                "reason": matched["reason"],
                "breakage": matched["breakage"],
                "source_evidence": [],
                "open_at_default_deny": matched["open"],
                "open_note": matched["open_note"],
                "exception_note": matched["exception_note"],
            },
        )
        for ev in evidence:
            if ev not in entry["source_evidence"]:
                entry["source_evidence"].append(ev)

    if unattributed:
        lines = "\n".join(
            f"  - {f.path}:{f.line_no} host={f.host} ({f.kind})「{f.raw[:80]}」"
            for f in unattributed
        )
        raise CensusError(
            "attribution できない外部宛先があります (ATTRIBUTION_RULES に足すこと):\n"
            + lines
        )

    rows = [
        Row(**{k: v for k, v in d.items()})
        for d in row_map.values()
    ]
    if include_provider_rows:
        rows.extend(build_provider_rows())
    seen: set[tuple[str, str, str]] = set()
    unique_rows: list[Row] = []
    for r in rows:
        k = (r.namespace, r.workload, r.endpoint)
        if k in seen:
            raise CensusError(f"台帳レコードが重複しています: {k}")
        seen.add(k)
        unique_rows.append(r)

    rows_sorted = sorted(unique_rows, key=lambda r: (_ns_sort_key(r.namespace), r.workload, r.endpoint))
    excluded_sorted = sorted(excluded_index.values(), key=lambda e: (e["category"], e["host"]))
    return rows_sorted, excluded_sorted


BLIND_SPOTS = [
    {
        "topic": "LLM API 実接続先の一部",
        "detail": "OPENCODE_API_KEY の向こう側の endpoint は repo 内に直書きが無い。"
        "api.anthropic.com は CLAUDE_CODE_OAUTH_TOKEN の provider 定数として載せたが、"
        "telemetry 系 (statsig 等) は確定できない",
    },
    {
        "topic": "Syncthing の global discovery / relay",
        "detail": "discovery.syncthing.net 等の宛先は GUI (PVC 上の config) で決まり "
        "repo に現れない。tailnet 直接接続のみで成立しているかは実測が必要 "
        "(実測プローブは次のプロジェクト)",
    },
    {
        "topic": "Vaultwarden の icon 取得",
        "detail": "icon cache が有効な場合、登録済みサイトの任意 host へ出ていく。"
        "repo には現れない",
    },
    {
        "topic": "Tailscale DERP relay",
        "detail": "*.derp.tailscale.com (UDP/TCP 443) は NAT 越えが必要なときだけ使う。"
        "coordination 本体 (controlplane.tailscale.com) とは別に開ける判断が要る",
    },
    {
        "topic": "coder の workspace agent 接続",
        "detail": "CODER_ACCESS_URL (https://coder.tailae6c2.ts.net) は公開 URL として"
        "self_public_url に除外したが、deployment.yaml のコメント通り workspace agent の"
        "接続先にもなる。既定拒否適用時は agent → coder の実経路を確認すること "
        "(実測プローブは次のプロジェクト)",
    },
    {
        "topic": "kubelet の image pull",
        "detail": "docker.io / ghcr.io からの pull は node 側 egress。既定拒否 "
        "NetworkPolicy では防げないので、統制は node firewall レイヤーで別途検討",
    },
]


def validate_census(rows: list[Row]) -> None:
    """DoD 下限と fail-closed 条件の最終検査 (実リポジトリの台帳に対して行う)。"""
    if len(rows) < MIN_ENDPOINTS:
        raise CensusError(
            f"endpoint 数が下限未満です: {len(rows)} < {MIN_ENDPOINTS}. "
            "走査か attribution が壊ている可能性"
        )
    hosts = {r.endpoint.split(":")[0] for r in rows}
    missing = MANDATORY_HOSTS - hosts
    if missing:
        raise CensusError(f"必須 host が台帳にありません: {sorted(missing)}")
    for r in rows:
        if not r.workload or not r.reason or not r.endpoint:
            raise CensusError(f"workload/reason/endpoint が欠けたレコードがあります: {r!r}")


# ---------------------------------------------------------------------------
# 出力 (JSON / Markdown)。決定的 (同一入力→同一バイト列) であることが --check の前提。
# ---------------------------------------------------------------------------


def render_json(rows: list[Row], excluded: list[dict]) -> str:
    doc = {
        "spec": "P-0203",
        "generated_by": "ops/security/egress_census.py",
        "method": "静的走査 (apps/**, nix/**, ops/rules.json)。実クラスタへの通信試験は含まない",
        "endpoints": [
            {
                "workload": r.workload,
                "namespace": r.namespace,
                "endpoint": r.endpoint,
                "reason": r.reason,
                "breakage": r.breakage,
                "source_evidence": list(r.source_evidence),
                "open_at_default_deny": r.open_at_default_deny,
                "open_note": r.open_note,
                "exception_note": r.exception_note,
            }
            for r in rows
        ],
        "excluded_hosts": excluded,
        "blind_spots": BLIND_SPOTS,
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def render_md(rows: list[Row], excluded: list[dict]) -> str:
    out: list[str] = []
    ap = out.append
    ap("# クラスタ外向き通信 (egress) の全数台帳 — P-0203\n")
    ap("> 生成: `python3 ops/security/egress_census.py` / 差分検査: `--check`。\n>"
       " 静的台帳であり**実クラスタへの通信試験は一切していない**。「manifest とコード上、"
       "どこへ出ていくはずか」の帳簿。到達性の実測は次のプロジェクト。\n")
    ap("## サマリ\n")
    ap("| namespace | endpoint 数 | 既定拒否で開けるべき穴 |")
    ap("|---|---|---|")
    by_ns: dict[str, list[Row]] = {}
    for r in rows:
        by_ns.setdefault(r.namespace, []).append(r)
    for ns in sorted(by_ns, key=_ns_sort_key):
        grp = by_ns[ns]
        must_open = sum(1 for r in grp if r.open_at_default_deny)
        ap(f"| {ns} | {len(grp)} | {must_open} |")
    ap("")
    ap("## namespace 別表\n")
    for ns in sorted(by_ns, key=_ns_sort_key):
        ap(f"### {ns}\n")
        ap("| workload | endpoint | 用途 | 既定拒否 | この穴が塞がれると壊れるもの |")
        ap("|---|---|---|---|---|")
        for r in by_ns[ns]:
            flag = "**開ける**" if r.open_at_default_deny else "不要 (管轄外)"
            ap(
                f"| `{r.workload}` | `{r.endpoint}` | {r.reason.replace('|', '\\|')} "
                f"| {flag} | {r.breakage.replace('|', '\\|')} |"
            )
        exceptions: dict[str, str] = {}
        for r in by_ns[ns]:
            if r.exception_note:
                exceptions.setdefault(r.workload, r.exception_note)
        if exceptions:
            ap("")
            ap("**autopilot namespace 対象外にする場合の例外理由文言:**\n")
            for w, note in exceptions.items():
                ap(f"- `{w}`: {note}")
            ap("")
        ap("")
    ap("## 横串: 主要依存が塞がれたときの被害一覧\n")
    ap("- **Doppler** (`api.doppler.com`) — External Secrets Operator の同期経路。塞がると"
       "全 namespace の Secret 更新が止まり、credential 回転が効かなくなる")
    ap("- **Backblaze B2** (`api.backblazeb2.com`) — restic backup 全 5 リポジトリの保存先。"
       "塞がるとバックアップも retention も失敗する")
    ap("- **GitHub** (`github.com` / `api.github.com`) — ArgoCD の manifest 取得、autopilot の"
       " git push、version-watcher / health-reporter / dashboard / telegram-adapter の API 呼び出し。"
       "塞がると宣言的運用と自律運用の双方が停止する")
    ap("- **Telegram** (`api.telegram.org`) — 人間からの指示窓口。塞がるとフィードバックが届かない")
    ap("- **Google OIDC 上流** (`accounts.google.com`) — Dex 経由の SSO 全不能")
    ap("- **Tailscale coordination** (`controlplane.tailscale.com`) — tailnet 参加資格そのもの。"
       "塞がると ts.net ingress 全滅")
    ap("- **コンテナレジストリ系** (`ghcr.io` / `docker.io`) — ArgoCD の OCI chart 取得と "
       "kubelet の image pull。pull は node 側で NetworkPolicy 管轄外")
    ap("- **Discord webhook** (`discord.com`) — autopilot の通知出口")
    ap("")
    ap("## 台帳から除外したホスト\n")
    ap("| host | 分類 | 理由 |")
    ap("|---|---|---|")
    for e in excluded:
        ev = " ".join(e["evidence"][:2])
        ap(f"| `{e['host']}` | {e['category']} | {e.get('reason', '')} ({ev}) |")
    ap("")
    ap("## 既知の盲点 (repo からは名前が取れない)\n")
    for bs in BLIND_SPOTS:
        ap(f"- **{bs['topic']}**: {bs['detail']}")
    ap("")
    return "\n".join(out)


def build_all() -> tuple[str, str]:
    """走査 → 構築 → DoD 下限検査 → レンダまで。戻り値は (json 文字列, md 文字列)。"""
    findings = scan_paths([APPS_DIR, NIX_DIR, RULES_PATH])
    rows, excluded = build_records(findings)
    validate_census(rows)
    return render_json(rows, excluded), render_md(rows, excluded)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="クラスタ外向き通信 (egress) の全数台帳を再生成/検査する (P-0203)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="再生成差分ゼロを確認する (ファイルは書き換えない)",
    )
    args = parser.parse_args()

    try:
        json_text, md_text = build_all()
    except CensusError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1

    if args.check:
        problems = []
        for path, want in ((OUT_JSON, json_text), (OUT_MD, md_text)):
            if not path.exists():
                problems.append(f"{path.relative_to(ROOT)} が存在しない")
                continue
            got = path.read_text(encoding="utf-8")
            if got != want:
                # 差分の要約だけ見せる (全 diff は git diff で確認できる)
                got_lines, want_lines = got.splitlines(), want.splitlines()
                first = next(
                    (
                        i
                        for i in range(max(len(got_lines), len(want_lines)))
                        if (got_lines[i] if i < len(got_lines) else "<EOF>")
                        != (want_lines[i] if i < len(want_lines) else "<EOF>")
                    ),
                    -1,
                )
                problems.append(
                    f"{path.relative_to(ROOT)} の再生成結果が異なる "
                    f"(行数 {len(got_lines)} → {len(want_lines)}, "
                    f"最初の差分行: {first + 1})。"
                    "python3 ops/security/egress_census.py を実行してから git diff で確認すること"
                )
        if problems:
            for p in problems:
                print(f"::error::{p}", file=sys.stderr)
            return 1
        n_eps = len(json.loads(json_text)["endpoints"])
        print(
            f"ok: egress 台帳は冪等です ({n_eps} endpoint / "
            f"{OUT_JSON.relative_to(ROOT)} と {OUT_MD.relative_to(ROOT)} が最新と一致)"
        )
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json_text, encoding="utf-8")
    OUT_MD.write_text(md_text, encoding="utf-8")
    n_eps = len(json.loads(json_text)["endpoints"])
    print(
        f"ok: egress 台帳を再生成しました ({n_eps} endpoint): "
        f"{OUT_JSON.relative_to(ROOT)}, {OUT_MD.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

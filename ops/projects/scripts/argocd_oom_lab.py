#!/usr/bin/env python3
"""
P-0196 — argocd OOM lab の構築・計測・判定ツール。

本番 argocd namespace には一切触れず、隔離 namespace (argocd-lab-916 / argocd-lab-1040)
に現行チャート (9.1.6 / app v3.2.1) と最新チャート (10.4.0 / app v3.5.1) を軽量化設定で
並べ、同一の合成 Application 負荷を与えて application-controller の working set を比較する。

標準ライブラリのみ (ops/check_version_sync.py と同じ方針)。--plan はクラスタ・ネットワークに
触れずに完了する (verify 第 1 項)。

権限上の制約と設計上の決定 (2026-08-23 実測。詳細は PROGRESS.md):

- kubectl は autopilot:autopilot-writer で動く。namespace 作成は可、Secret/Role/RoleBinding/
  ClusterRole の作成は不可。
- chart の Secret (argocd-secret / argocd-redis) は適用対象から除外し、ExternalSecret
  (既存 ClusterSecretStore doppler 参照 + ダミー remoteRef 1 本 + template 固定値) で
  ESO に作らせる。実測 t+10s で synced。値は lab 専用のダミーであり実 credential ではない。
  webhook が data/dataFrom 必須としているため remoteRef は削除できない (実測)。
- CRD / ClusterRole / ClusterRoleBinding / redis-secret-init Job (helm hook) はクラスタ
  スコープのため適用対象から除外。Application CRD 等は本番が既に提供している。
- 名前空間スコープの Role では Deployment 等への書き込みが不可 (render 実測: get/list/watch
  のみ) → syncPolicy は manual。計測対象は refresh → manifest generate → diff → status 更新の
  定常 reconcile ループであり、sync 実行時のメモリスパイクは再現しない (限界として verdict
  に記載)。prod OOM が数時間稼働後の restarts 4 という経過であることと整合。
- server / applicationset-controller は chart 9.1.6 に enabled スイッチが存在しないため
  (values 実測)、post-render フィルタで Deployment と Service を落とした manifest を
  rendered/ 配下にコミットして使う。ランタイムに helm は不要。
- NetworkPolicy は新チャートのみ 4 個付属するため非対称。公平性のため両系統で適用しない。
- chart 由来の ServiceAccount/Role/RoleBinding (rendered 各 27 docs 中 15 docs) は
  autopilot-writer に作れないため、人間が proposed-rbac-for-human.yaml を適用する前提。
  up 冒頭で admission probe (SA 参照 Pod を 1 個建てる) により適用済みかを検証する —
  writer は serviceaccounts の get も不可なので直接確認できない (Forbidden ≠ 不存在。
  セッション 2 の CRD 誤検知と同型の罠)。未適用なら up は即中断し適用コマンドを出す。
  controller の workload が参照する SA 名は提案 YAML 側 (argocd-lab-application-controller)
  に apply 時に書き換えて合わせる (提案ファイル自体は既に「提案済み」なので触らない)。
- AppProject default は chart が作らないため up が各 ns に作る (destinations 自 ns 限定、
  clusterResourceWhitelist 空 = cluster-scoped 拒否)。無いと reconcile が project 検証で
  短路する恐れがある (PROGRESS.md セッション 2)。

判定基準 (cmd_verdict):
- leak            : いずれかの系統で傾き > LEAK_SLOPE_MIB_PER_H が持続、または計測窓内に
                    controller の restart 数が増加 (lab limits 1Gi での OOMKill は強信号)。
                    両系統ともリークなら chart 非依存の 'leak'、片方のみなら乖離なので
                    'chart-regression'。
- insufficient-request : 両系統とも平坦 (リーク閾値未満) で、平台値が本番 limit
                    (PROD_LIMIT_MIB) × INSUFFICIENT_RATIO 以上。
- chart-regression: リーク無しでも新旧の平台値の相対差が DIVERGENCE_RATIO 以上。
- どれにも該当せず判定が不能な場合は verdict.json を書かず非 0 終了 (負荷を増やす等の
  再測定指示)。3 分類への無理な当てはめはしない。
"""
import argparse
import csv
import datetime
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LAB_ROOT = ROOT / "ops" / "projects" / "logs" / "argocd-oom-lab"
RENDERED_DIR = LAB_ROOT / "rendered"
CSV_PATH = LAB_ROOT / "rss_series.csv"
VERDICT_PATH = LAB_ROOT / "verdict.json"

NS_OLD = "argocd-lab-916"
NS_NEW = "argocd-lab-1040"
SYSTEMS = [
    {"key": "old", "ns": NS_OLD, "chart": "9.1.6", "app": "v3.2.1",
     "file": "argocd-916.yaml",
     "tgz_url": "https://github.com/argoproj/argo-helm/releases/download/argo-cd-9.1.6/argo-cd-9.1.6.tgz",
     "sha256": "3ff4f2b2f97b68cc7ee75d469a8283c09005cef7e35e6d7488881ba0867e4fe8"},
    {"key": "new", "ns": NS_NEW, "chart": "10.4.0", "app": "v3.5.1",
     "file": "argocd-1040.yaml",
     "tgz_url": "https://github.com/argoproj/argo-helm/releases/download/argo-cd-10.4.0/argo-cd-10.4.0.tgz",
     "sha256": "5abb71c17bc082e13dc3d90023972f871ea8e1dfc26d8f3218ceade215b971d5"},
]

APPS_PER_SYSTEM = 30
SOURCE_REPO = "https://github.com/argoproj/argocd-example-apps.git"
EXCLUDE_PATHS = {"apps", "hack"}  # apps は app-of-apps で外部へ子 Application を撒くため除外
EXCLUDE_PATH_RE = re.compile(r"unhealthy|broken|invalid|skip")

INTERVAL_MIN = 15
WINDOW_HOURS = 4
MIN_SAMPLES = 8

PROD_LIMIT_MIB = 512.0          # apps/argocd/values.yaml controller.limits.memory
INSUFFICIENT_RATIO = 0.9        # 平台値が limit の 9 割以上なら「足りない」側とみなす
LEAK_SLOPE_MIB_PER_H = 15.0     # 半分の窓での線形回帰傾きがこれを超え続けたらリーク
DIVERGENCE_RATIO = 0.25         # 新旧平台値の相対差の有意閾値

SECRETKEY_DUMMY = "p0196-lab-dummy-server-secretkey-not-real"
REDIS_PASSWORD_DUMMY = "p0196-lab-dummy-redis-password-not-real"

# 再 render 用の values (rendered/lab-values.yaml に同一物を置く)。
# 再生成手順: helm template argocd-lab <tgz> --namespace <ns> --skip-crds
#             -f rendered/lab-values.yaml → filter_manifest() → rendered/<file>
# controller.repo.server.plaintext: lab では server 無効のため argocd-repo-server-tls を
# 作る者がいない。chart はこの Secret を optional: true で mount する (chart 標準) ので
# pod 起動は阻害されないが、通信モードを plaintext に明示固定しないと controller 既定
# (TLS) と repo-server 実挙動 (証明書無し→平文) の組み合わせが version 既定値に依存する。
LAB_VALUES = """global:
  domain: argocd-lab.local
dex:
  enabled: false
notifications:
  enabled: false
applicationSet:
  enabled: false
server:
  enabled: false
configs:
  params:
    server.insecure: true
    controller.repo.server.plaintext: "true"
controller:
  resources:
    requests:
      cpu: 50m
      memory: 256Mi
    limits:
      cpu: 500m
      memory: 1Gi
repoServer:
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: 250m
      memory: 512Mi
redis:
  resources:
    requests:
      cpu: 25m
      memory: 32Mi
    limits:
      cpu: 100m
      memory: 128Mi
"""

CSV_HEADER = ["timestamp_utc", "system", "pod", "phase", "restart_count",
              "top_cpu_m", "top_memory_mi", "node_cpu_pct", "node_mem_pct", "note"]

CLUSTER_SCOPED_DROP = {"CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding"}
# chart 9.1.6 に disable スイッチが無いため post-render で落とす workload。
# 名前の suffix ではなく app.kubernetes.io/name ラベルで判定する
# (「-server」suffix では repo-server も誤って落ちる — 実際に起きたバグ)。
DROP_WORKLOAD_LABELS = {"argocd-server", "argocd-applicationset-controller"}
LABEL_RE = re.compile(r"app\.kubernetes\.io/name:\s*(\S+)")

# writer が作れないため人間適用 (proposed-rbac-for-human.yaml) に外部化する kinds。
# apply 時に除去しないと kubectl apply の GET で Forbidden になり部分適用が残る
# (セッション 2 実測)。適用可能なのは 27 docs 中 12 docs。
EXTERNAL_RBAC_KINDS = {"ServiceAccount", "Role", "RoleBinding"}

# 人間適用 RBAC が供給する SA 名。workload からの参照はこの集合に正規化して assert する
SA_CONTROLLER = "argocd-lab-application-controller"
SA_REPO_SERVER = "argocd-lab-repo-server"
ALLOWED_SA_REFS = {"default", SA_CONTROLLER, SA_REPO_SERVER}
SAREF_RE = re.compile(r"serviceAccountName:\s*(\S+)")
PROBE_POD_NAME = "p0196-rbac-probe"


def run(cmd, check=True, capture=True, input=None):
    p = subprocess.run(cmd, capture_output=capture, text=True, input=input)
    if check and p.returncode != 0:
        raise RuntimeError(f"command failed rc={p.returncode}: {' '.join(cmd)}\n{p.stderr}")
    return p


def kubectl(*args, check=True):
    return run(["kubectl", *args], check=check)


# --- manifest 操作 -----------------------------------------------------------

def split_docs(text):
    return [d for d in re.split(r"^---\s*$", text, flags=re.M) if d.strip()]


def doc_meta(doc):
    kind = re.search(r"^kind:\s*(\S+)", doc, re.M)
    name = re.search(r"^  name:\s*(\S+)", doc, re.M)
    ns = re.search(r"^  namespace:\s*\"?([\w-]+)\"?", doc, re.M)
    return (kind.group(1) if kind else "?",
            name.group(1) if name else "?",
            ns.group(1) if ns else "")


def filter_manifest(text, system_ns):
    """レンダリング済み manifest から適用対象外を落とす。

    クラスタスコープ物・Secret・無効化 workload に加え、SA/Role/RoleBinding は
    人間適用の RBAC (proposed-rbac-for-human.yaml) が供給するため除外する。
    """
    kept, dropped = [], []
    for doc in split_docs(text):
        kind, name, ns = doc_meta(doc)
        drop = False
        if kind in CLUSTER_SCOPED_DROP or kind == "Secret":
            drop = True
        elif kind in EXTERNAL_RBAC_KINDS:
            drop = True  # 人間適用分と重複。apply の GET Forbidden を避けるため落とす
        elif kind == "Job":
            drop = True  # redis-secret-init (helm hook) — Secret は ExternalSecret で供給
        elif kind == "NetworkPolicy":
            drop = True  # 新チャートのみ付属。公平性のため両系統で使わない
        elif kind in ("Deployment", "Service"):
            m = LABEL_RE.search(doc)
            drop = bool(m and m.group(1) in DROP_WORKLOAD_LABELS)
        if drop:
            dropped.append(f"{kind}/{name}")
        else:
            kept.append(doc.rstrip() + "\n")
    return kept, dropped


def align_sa_refs(docs):
    """workload の serviceAccountName を人間適用 RBAC の SA 名に揃える。

    chart 由来 STS は argocd-application-controller を参照するが提案 YAML は
    argocd-lab-application-controller を作る (repo-server 提案名は chart 名と一致済み)。
    提案ファイルは既に「提案済み」のためコード側で合わせる。
    戻り値: 書換後 docs, 書換リスト [(old, new)]
    """
    old2new = {"argocd-application-controller": SA_CONTROLLER}
    rewritten = []
    out = []
    for doc in docs:
        m = SAREF_RE.search(doc)
        if m and m.group(1) in old2new and m.group(1) not in ALLOWED_SA_REFS:
            new = old2new[m.group(1)]
            doc = SAREF_RE.sub(f"serviceAccountName: {new}", doc, count=1)
            rewritten.append((m.group(1), new))
        out.append(doc)
    return out, rewritten


def assert_sa_refs(docs):
    bad = []
    for doc in docs:
        for m in SAREF_RE.finditer(doc):
            if m.group(1) not in ALLOWED_SA_REFS:
                kind, name, _ = doc_meta(doc)
                bad.append(f"{kind}/{name}: serviceAccountName={m.group(1)}")
    if bad:
        raise SystemExit("人間適用 RBAC に無い SA への参照が残っている:\n  " + "\n  ".join(bad))


def gen_appproject(ns):
    """chart は default project を作らない。destinations を自 ns に限定した
    AppProject default を置く (提案 YAML 冒頭コメントの「別レイヤーで塞ぐ」の実装)。"""
    return f"""apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: default
  namespace: {ns}
  labels:
    app.kubernetes.io/part-of: p0196-argocd-oom-lab
spec:
  sourceRepos: ["*"]
  sourceNamespaces: ["{ns}"]
  destinations:
    - server: https://kubernetes.default.svc
      namespace: {ns}
  clusterResourceWhitelist: []
"""


def probe_rbac(ns):
    """lab 用 SA/Role が効いているかを writer の権限内で確かめる。

    serviceaccounts の get ができないため、SA 参照 Pod を 1 個作って admission
    (ServiceAccount プラグイン) に存在判定を委ねる。SA が無ければ create 自体が
    拒否され 'serviceaccount ... not found' が返る。戻り値: (ok, message)。
    ok 時に生成した probe pod は呼び出し側が削除する。
    """
    probe = f"""apiVersion: v1
kind: Pod
metadata:
  name: {PROBE_POD_NAME}
  namespace: {ns}
  labels:
    app.kubernetes.io/part-of: p0196-argocd-oom-lab
spec:
  serviceAccountName: {SA_CONTROLLER}
  restartPolicy: Never
  containers:
  - name: pause
    image: registry.k8s.io/pause:3.10
"""
    r = run(["kubectl", "create", "-f", "-", "--dry-run=client"],
            check=False, input=probe)  # まず構文確認 (クラスタ非接触)
    if r.returncode != 0:
        return False, f"probe manifest が不正: {r.stderr}"
    r = run(["kubectl", "create", "-f", "-"], check=False, input=probe)
    if r.returncode != 0:
        err = r.stderr.strip()
        if "serviceaccount" in err.lower() and "not found" in err.lower():
            return False, f"{ns}: SA '{SA_CONTROLLER}' が無い — lab 用 RBAC 未適用"
        return False, f"{ns}: probe pod の作成に失敗 (想定外): {err}"
    kubectl("delete", "pod", PROBE_POD_NAME, "-n", ns,
            "--ignore-not-found=true", "--wait=true")
    return True, f"{ns}: admission probe 通過 — RBAC 適用済み"


def wait_ns_gone(ns, timeout_s=60):
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout_s)
    while datetime.datetime.now() < deadline:
        if kubectl("get", "namespace", ns, check=False).returncode != 0:
            return True
        run(["sleep", "3"])
    return False


def load_rendered(system):
    path = RENDERED_DIR / system["file"]
    if not path.exists():
        raise SystemExit(f"{path} が無い。rendered 済み manifest がコミットされているはず")
    docs = split_docs(path.read_text())
    bad = [f"{k}/{n}" for k, n, _ in map(doc_meta, docs)
           if k in CLUSTER_SCOPED_DROP or k == "Secret"]
    if bad:
        raise SystemExit(f"{path}: 適用禁止オブジェクトが混入している {bad}")
    return path, len(docs)


def external_secret_yaml(ns, target_name, pairs):
    """ESO 経由で Secret を作る ExternalSecret。

    webhook が data/dataFrom 必須のため、実在する Doppler キーへの remoteRef を 1 本だけ
    置くが、template.data を明示しているのでリモート値は最終 Secret に反映されない
    (ESO のテンプレート仕様: template.data 指定時はそのキーのみ出力される)。
    """
    data = "\n".join(f"        {k}: \"{v}\"" for k, v in pairs)
    return f"""apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {target_name}-provisioner
  namespace: {ns}
  labels:
    app.kubernetes.io/part-of: p0196-argocd-oom-lab
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: doppler
  target:
    name: {target_name}
    creationPolicy: Owner
    template:
      data:
{data}
  data:
    - secretKey: src-unused
      remoteRef:
        key: DEX_ARGOCD_CLIENT_SECRET
"""


def gen_application(name, system_ns, source_sha, path):
    return f"""apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
  namespace: {system_ns}
  labels:
    app.kubernetes.io/part-of: p0196-argocd-oom-lab
spec:
  project: default
  source:
    repoURL: {SOURCE_REPO}
    targetRevision: {source_sha}
    path: {path}
  destination:
    server: https://kubernetes.default.svc
    namespace: {system_ns}
  syncPolicy: {{}}
"""


def resolve_source_paths():
    """合成負荷のソースリポジトリを clone し、manifest を含むパスを列挙する。

    up 時点の HEAD SHA を使い、両系統が同一 SHA を参照することだけを担保する
    (比較実験として重要。どの時点の SHA かは lab-state.json に残る)。
    戻り値: (sha, paths)
    """
    with tempfile.TemporaryDirectory(prefix="p0196-src.") as td:
        repo = Path(td) / "src"
        run(["git", "clone", "--quiet", SOURCE_REPO, str(repo)])
        head = run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
        sha = head
        paths = []
        for child in sorted(repo.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in EXCLUDE_PATHS or EXCLUDE_PATH_RE.search(child.name.lower()):
                continue
            files = {f.name for f in child.rglob("*") if f.is_file()}
            if files & {"Chart.yaml", "kustomization.yaml"} or any(
                    f.endswith((".yaml", ".yml", ".jsonnet")) for f in files):
                paths.append(child.name)
        if not paths:
            raise SystemExit("ソースリポジトリに利用可能なパスが無い")
        return sha, paths


def gen_applications(source_sha, paths):
    """両系統同一の 30 本。ユニークパスが 30 未満なら接尾辞付きで反復する (sync しないので
    リソース名の衝突は起きない)。"""
    apps = []
    for i in range(APPS_PER_SYSTEM):
        path = paths[i % len(paths)]
        suffix = "" if i < len(paths) else f"-r{i // len(paths) + 1}"
        for key, sysinfo in (("old", SYSTEMS[0]), ("new", SYSTEMS[1])):
            apps.append((key, gen_application(f"load-{i:02d}-{path.replace('/', '-')}{suffix}",
                                              sysinfo["ns"], source_sha, path)))
    return apps


# --- サブコマンド ------------------------------------------------------------

def cmd_plan(_args):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    a = lines.append
    a(f"P-0196 argocd OOM lab — plan ({now}, オフライン生成・クラスタ非接触)")
    a("")
    a("## 目的")
    a("  本番 application-controller (exit 137 OOMKilled, restarts 4) の原因を")
    a("  「512Mi が足りない」のか「リーク」なのかに分類し、同時に chart 9.1.6 → 10.4.0")
    a("  の更新判断材料を得る。policy=manual 凍結中の argocd-chart に対する初めての実測根拠。")
    a("")
    a("## 系統構成 (隔離 namespace 2 套、本番 argocd ns とは完全分離)")
    a(f"  {'系統':<5} {'namespace':<16} {'chart':<8} {'appVer':<8} {'controller limits'}")
    for s in SYSTEMS:
        a(f"  {s['key']:<5} {s['ns']:<16} {s['chart']:<8} {s['app']:<8} cpu 500m / mem 1Gi")
    a("")
    a("## 前提 (人間の一手)")
    a("  - proposed-rbac-for-human.yaml の適用 (lab ns x2 + SA x2/ns + Role/RoleBinding)。")
    a("    writer は RBAC を作れない (設計)。up 冒頭で admission probe により適用済みかを")
    a("    自動判定し、未適用なら中断して適用コマンドを出す")
    a("")
    a("## 作成するオブジェクト")
    a(f"  - Namespace x2            : {NS_OLD}, {NS_NEW}")
    a("  - ExternalSecret x4       : 各系統 2 本。ESO (既存 ClusterSecretStore doppler 参照)")
    a("                              が argocd-secret(server.secretkey=ダミー固定値) と")
    a("                              argocd-redis(auth=ダミー固定値) を作る。")
    a("                              実 credential は使わない (template 固定値)")
    for s in SYSTEMS:
        try:
            path, n = load_rendered(s)
            kept, dropped = filter_manifest(path.read_text(), s["ns"])
            n_ext = sum(1 for d in dropped if d.split("/")[0] in EXTERNAL_RBAC_KINDS)
            a(f"  - rendered objects x{len(kept):<3}: {s['ns']} ({s['file']} — 全 {n} docs 中")
            a(f"                              SA/Role/RoleBinding {n_ext} は人間適用分のため除外)")
        except SystemExit as e:
            a(f"  - rendered objects      : {e}")
    a("  - AppProject default x2   : 各系統 1 本。destinations を自 ns に限定、")
    a("                              clusterResourceWhitelist 空 = cluster-scoped 拒否")
    a(f"  - Application x{APPS_PER_SYSTEM * 2:<3}: 各系統 {APPS_PER_SYSTEM} 本 (両系統同一定義)")
    a("")
    a("## 意図的に作らないもの (実測に基づく除外)")
    a("  - Secret 直適用           : autopilot-writer に create secrets 不可 (can-i 実測 no)")
    a("  - CRD                     : クラスタスコープ。本番の applications.argoproj.io 等を流用")
    a("  - ClusterRole/Binding     : 同上。名前空間 Role のみで運用できる設計にする")
    a("  - SA/Role/RoleBinding     : writer に作れない (rbac.authorization.k8s.io 不付与 =")
    a("                              「自分の権限を自分で広げる経路を作らない」)。人間適用の")
    a("                              proposed-rbac-for-human.yaml が供給するため除外")
    a("  - server/appset Deployment: chart 9.1.6 に enabled スイッチ無し (values 実測) につき")
    a("                              post-render フィルタで除去済み")
    a("  - NetworkPolicy           : 新チャートのみ付属 (4 個)。非対称なので両系統で不使用")
    a("  - redis-secret-init Job   : helm hook。argocd-redis は ExternalSecret で代替")
    a("")
    a("## 負荷設計")
    a(f"  - source : {SOURCE_REPO}")
    a("  - SHA    : up 時に解決し両系統で同一 SHA を使う (公平性の要)")
    a("  - パス   : manifest を含むトップレベル dir を列挙 (apps/hack と unhealthy* 等は除外)。")
    a("  - sync   : manual — 名前空間 Role に Deployment 書き込み権が無い (render 実測) ため。")
    a("             計測対象は refresh→generate→diff→status の定常ループ。")
    a("             30 本は prod 実測 16 本の約 2 倍。reconcile 周期は chart 既定 (180s)")
    a("")
    a("## サンプリング計画")
    a(f"  - 間隔 {INTERVAL_MIN} 分 × 窓 {WINDOW_HOURS} 時間 (複数 reconcile 周期をカバー)")
    a(f"  - CSV    : {CSV_PATH.relative_to(ROOT)}")
    a("    schema : " + ",".join(CSV_HEADER))
    a("    top 値は metrics-server の working set (RSS 近似) であることを verdict に明記")
    a("  - node CPU/MEM % を同時刻で併記 (ノードが飽和すると曲線の解釈が変わるため)")
    a("")
    a("## 判定基準 (verdict サブコマンド)")
    a(f"  - leak                 : 傾き > {LEAK_SLOPE_MIB_PER_H} MiB/h 持続 or 計測窓内 restart 増加")
    a(f"  - insufficient-request : 平坦 & 平台値 >= {PROD_LIMIT_MIB:.0f}Mi x {INSUFFICIENT_RATIO:.0%}")
    a(f"  - chart-regression     : 新旧平台値の相対差 >= {DIVERGENCE_RATIO:.0%} (または片側のみリーク)")
    a("  - 不判定               : verdict.json を書かず非 0 終了 (無理な分類をしない)")
    a("")
    a("## up の前段チェック (up 実行時に確認)")
    a("  - kubectl auth can-i create/delete namespaces, create appprojects")
    a("  - node01 の空き (top nodes): MEM 使用率 90% 超で警告、95% 超で中断")
    a("  - CRD 存在確認 (applications/appprojects/applicationsets.argoproj.io)")
    a("  - lab 用 RBAC 適用済み確認 (admission probe — get sa 不可のため直接確認は不可能)")
    a("  - rendered manifest の禁止オブジェクト混入チェック (load_rendered)")
    a("")
    a("## 削除計画 (down)")
    a(f"  - Namespace 2 個を削除し消失を確認。CRD は本番共有のため絶対に削除しない")
    print("\n".join(lines))


def preflight():
    problems = []
    if kubectl("auth", "can-i", "create", "namespaces").stdout.strip() != "yes":
        problems.append("namespace の作成権限がない")
    if kubectl("auth", "can-i", "delete", "namespaces").stdout.strip() != "yes":
        problems.append("namespace の削除権限がない (lab の掃除ができない)")
    if kubectl("auth", "can-i", "create",
               "appprojects.argoproj.io").stdout.strip() != "yes":
        problems.append("appproject の作成権限がない (project 検証を通せない)")
    # CRD の get/list は autopilot-writer に許可されていない (Forbidden = 存在しない
    # ではない)。discovery API 経由なら全認証済み主体が見えるので api-resources で確認する
    api_res = run(["kubectl", "api-resources", "--api-group=argoproj.io"],
                  check=False).stdout
    for res in ("applications", "appprojects", "applicationsets"):
        if not re.search(rf"(?m)^\s*{res}\s", api_res):
            problems.append(f"argoproj.io の {res} が discovery に無い (流用前提が崩れた)")
    out = kubectl("top", "nodes", "--no-headers").stdout.split()
    # NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%
    mem_pct = float(out[4].rstrip("%"))
    if mem_pct > 95:
        problems.append(f"node メモリ使用率 {mem_pct}% — lab 投入を中断")
    elif mem_pct > 90:
        print(f"[warn] node メモリ使用率 {mem_pct}% — 追加投入は曲線の解釈に影響しうる", file=sys.stderr)
    return problems


def cmd_up(_args):
    problems = preflight()
    if problems:
        raise SystemExit("preflight 失敗:\n  " + "\n  ".join(problems))
    print("[preflight] ok")

    sha, paths = resolve_source_paths()
    state = {"source_sha": sha, "paths": paths,
             "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    print(f"[source] sha={sha[:12]} paths={len(paths)}: {', '.join(paths)}")

    # lab-state.json は RBAC probe 通過後まで書かない (未適用のまま中断したとき
    # verdict の source_sha 参照が汚れる — セッション 2 の後始末で判明)
    with tempfile.TemporaryDirectory(prefix="p0196-up.") as td:
        for s in SYSTEMS:
            ns = s["ns"]
            created_here = kubectl("create", "namespace", ns, check=False).returncode == 0
            ok, msg = probe_rbac(ns)
            if not ok:
                if created_here:
                    kubectl("delete", "namespace", ns, "--ignore-not-found=true")
                    wait_ns_gone(ns)
                    print(f"[cleanup] 自分が今作った空 namespace {ns} を削除")
                raise SystemExit(msg + "\n"
                                 "  人間が次を適用するのを待つ (トークン複製等の自動回避はしない):\n"
                                 "    kubectl apply -f ops/projects/logs/argocd-oom-lab/"
                                 "proposed-rbac-for-human.yaml")
            print(f"[rbac] {msg}")

            es_path = Path(td) / f"es-{ns}.yaml"
            es_path.write_text(
                external_secret_yaml(ns, "argocd-secret",
                                     [("server.secretkey", SECRETKEY_DUMMY)]))
            run(["kubectl", "apply", "-f", str(es_path)])
            es2_path = Path(td) / f"es-redis-{ns}.yaml"
            es2_path.write_text(
                external_secret_yaml(ns, "argocd-redis",
                                     [("auth", REDIS_PASSWORD_DUMMY)]))
            run(["kubectl", "apply", "-f", str(es2_path)])

    (LAB_ROOT / "lab-state.json").write_text(json.dumps(state, indent=2))

    print("[secrets] ExternalSecret 適用済み — Ready 待ち...")
    for s in SYSTEMS:
        for target in ("argocd-secret", "argocd-redis"):
            for _ in range(18):
                r = kubectl("get", "externalsecret", f"{target}-provisioner",
                            "-n", s["ns"], "-o",
                            "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                            check=False)
                if r.stdout.strip() == "True":
                    break
                run(["sleep", "10"])
            else:
                raise SystemExit(f"{s['ns']}/{target} が synced にならない")

    for s in SYSTEMS:
        ns = s["ns"]
        path, n_total = load_rendered(s)
        kept, dropped = filter_manifest(path.read_text(), ns)
        kept, rewritten = align_sa_refs(kept)
        assert_sa_refs(kept)
        filtered = Path(tempfile.mkstemp(prefix=f"p0196-filtered-{ns}.")[1])
        try:
            filtered.write_text("\n---\n".join(kept))
            run(["kubectl", "apply", "-n", ns, "-f", str(filtered)])
        finally:
            filtered.unlink()
        n_ext = sum(1 for d in dropped if d.split("/")[0] in EXTERNAL_RBAC_KINDS)
        print(f"[apply] {ns}: {len(kept)} objects "
              f"(SA/Role/RoleBinding {n_ext} は人間適用分のため除外"
              + (f"、SA 参照を書換: {', '.join(f'{a}->{b}' for a, b in rewritten)}"
                 if rewritten else "") + ")")

    for s in SYSTEMS:
        ns = s["ns"]
        with tempfile.TemporaryDirectory(prefix="p0196-proj.") as td:
            p = Path(td) / "appproject.yaml"
            p.write_text(gen_appproject(ns))
            run(["kubectl", "apply", "-f", str(p)])
        print(f"[project] {ns}: AppProject default (destinations 自 ns 限定)")

    apps = gen_applications(sha, paths)
    with tempfile.TemporaryDirectory(prefix="p0196-apps.") as td:
        tmpdir = Path(td)
        by_key = {}
        for key, yaml_text in apps:
            p = tmpdir / f"{key}-{len(by_key)}.yaml"
            p.write_text(yaml_text)
            by_key.setdefault(key, []).append(p)
        for key, files in by_key.items():
            ns = next(x["ns"] for x in SYSTEMS if x["key"] == key)
            for p in files:
                run(["kubectl", "apply", "-f", str(p)])
            print(f"[apps] {ns}: {len(files)} Applications")

    print("\n次: 数時間待って sample → 計画窓を満たしたら verdict")
    print(f"  watch: python3 {Path(__file__).name} sample --note \"...\"")


def collect_system(sysinfo, node_cpu, node_mem, note):
    ns = sysinfo["ns"]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pods = {}
    try:
        data = json.loads(kubectl("get", "pods", "-n", ns, "-o", "json").stdout)
    except RuntimeError:
        return []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        phase = item.get("status", {}).get("phase", "?")
        restarts = sum(c.get("restartCount", 0)
                       for c in item.get("status", {}).get("containerStatuses", []))
        pods[name] = [now, sysinfo["key"], name, phase, restarts, "", "",
                      node_cpu, node_mem, note]
    top = kubectl("top", "pods", "-n", ns, "--no-headers", check=False).stdout
    for line in top.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in pods:
            pods[parts[0]][5] = parts[1].rstrip("m") or ""
            pods[parts[0]][6] = parts[2].removesuffix("Mi") or ""
    return list(pods.values())


def cmd_sample(args):
    rows = []
    node_cpu = node_mem = ""
    top_nodes = kubectl("top", "nodes", "--no-headers", check=False).stdout.split()
    if len(top_nodes) >= 5:
        node_cpu, node_mem = top_nodes[2].rstrip("%"), top_nodes[4].rstrip("%")
    for s in SYSTEMS:
        rows.extend(collect_system(s, node_cpu, node_mem, args.note or ""))
    if not rows:
        raise SystemExit("サンプルが取れない — lab が立っているか確認して (up / status)")
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerows(rows)
    print(f"[sample] {len(rows)} rows -> {CSV_PATH.relative_to(ROOT)}"
          + ("" if not args.note else f" (note: {args.note})"))


def read_series():
    if not CSV_PATH.exists():
        raise SystemExit(f"{CSV_PATH} が無い — 先に up & sample")
    series = {}
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            try:
                mem = float(row["top_memory_mi"])
            except (ValueError, KeyError):
                continue
            if "controller" not in row["pod"]:
                continue
            series.setdefault(row["system"], []).append(
                (row["timestamp_utc"], mem, int(row["restart_count"] or 0)))
    return series


def parse_ts(t):
    return datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))


def series_stats(points):
    pts = sorted(points)
    ys = [m for _, m, _ in pts]
    n = len(pts)
    half = pts[n // 2:] if n >= 4 else pts
    xs_h = [parse_ts(t).timestamp() / 3600.0 for t, _, _ in half]
    yh = [m for _, m, _ in half]
    mx, my = statistics.mean(xs_h), statistics.mean(yh)
    denom = sum((x - mx) ** 2 for x in xs_h)
    slope = (sum((x - mx) * (y - my) for x, y in zip(xs_h, yh)) / denom) if denom else 0.0
    tail = ys[-max(3, n // 3):]
    restart_delta = pts[-1][2] - pts[0][2]
    span_h = ((parse_ts(pts[-1][0]).timestamp() - parse_ts(pts[0][0]).timestamp()) / 3600.0)
    return {
        "samples": n, "window_hours": round(span_h, 3),
        "min_mib": round(min(ys), 1), "median_mib": round(statistics.median(ys), 1),
        "max_mib": round(max(ys), 1),
        "plateau_mib": round(statistics.median(tail), 1),
        "slope_mib_per_h": round(slope, 2),
        "restart_start": pts[0][2], "restart_end": pts[-1][2],
        "restart_delta": restart_delta,
    }


def classify(stats):
    old, new = stats["old"], stats["new"]
    notes = []

    def is_leak(st):
        return st["slope_mib_per_h"] > LEAK_SLOPE_MIB_PER_H or st["restart_delta"] > 0

    o_leak, n_leak = is_leak(old), is_leak(new)
    if o_leak and n_leak:
        return "leak", notes + ["両系統ともリーク様相 — chart 非依存"]
    if o_leak != n_leak:
        side = "旧(9.1.6)" if o_leak else "新(10.4.0)"
        return "chart-regression", notes + [f"片側のみリーク様相: {side}"]
    plateau_gap = abs(old["plateau_mib"] - new["plateau_mib"]) / max(old["plateau_mib"], new["plateau_mib"])
    if plateau_gap >= DIVERGENCE_RATIO:
        hi = "新(10.4.0)" if new["plateau_mib"] > old["plateau_mib"] else "旧(9.1.6)"
        return "chart-regression", notes + [f"平台値乖離 {plateau_gap:.0%} — 高い側: {hi}"]
    lo = min(old["plateau_mib"], new["plateau_mib"])
    if lo >= PROD_LIMIT_MIB * INSUFFICIENT_RATIO:
        return "insufficient-request", notes + [
            f"両系統平坦 & 平台値 {lo:.0f}Mi >= 512Mi x {INSUFFICIENT_RATIO:.0%}"]
    return None, notes + [
        f"平坦かつ平台値 {lo:.0f}Mi < 512Mi x {INSUFFICIENT_RATIO:.0%} — 合成負荷が本番の"
        f"所要に届いていない可能性。負荷量 (現 {APPS_PER_SYSTEM} 本) か reconcile 周期を"
        "上げて再測定するか、窓を延長すること"]


def cmd_verdict(_args):
    series = read_series()
    missing = [s["key"] for s in SYSTEMS if s["key"] not in series]
    if missing:
        raise SystemExit(f"系列が足りない: {missing}")
    short = [k for k, v in series.items() if len(v) < MIN_SAMPLES]
    if short:
        raise SystemExit(f"サンプル不足 (<{MIN_SAMPLES}): {short} — 窓を延長して sample を続ける")
    stats = {k: series_stats(v) for k, v in sorted(series.items())}
    conclusion, notes = classify(stats)
    if conclusion is None:
        print("[verdict] 判定不能:", file=sys.stderr)
        for n in notes:
            print("  " + n, file=sys.stderr)
        raise SystemExit(2)
    try:
        csv_ref = str(CSV_PATH.relative_to(ROOT))
    except ValueError:  # テスト等で ROOT 外の CSV を指す場合
        csv_ref = str(CSV_PATH)

    state_path = LAB_ROOT / "lab-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    verdict = {
        "conclusion": conclusion,
        "rss_series_csv": csv_ref,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "measurement": {
            "interval_min": INTERVAL_MIN,
            "metric": "kubectl top pod (metrics-server working set — RSS の近似)",
            "per_system": stats,
        },
        "load": {
            "apps_per_system": APPS_PER_SYSTEM,
            "source_repo": SOURCE_REPO,
            "source_sha": state.get("source_sha"),
            "sync_policy": "manual (refresh/generate/diff/status のみ。sync 実行は対象外)",
            "reconcile_timeout_s": 180,
        },
        "lab_limits": {
            "controller": {"cpu": "500m", "memory": "1Gi"},
            "note": "被験体を途中で殺さない上限 (T-0055)。prod 512Mi より広く、"
                    "1Gi 到達=OOMKill は結果として記録される",
        },
        "notes": notes,
        "excluded_by_design": [
            "sync 実行時のメモリスパイク (RBAC 制約)",
            "server / applicationset / notifications / dex (軽量化)",
            "NetworkPolicy (新旧非対称のため双方不使用)",
        ],
    }
    VERDICT_PATH.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n")
    try:
        verdict_ref = VERDICT_PATH.relative_to(ROOT)
    except ValueError:
        verdict_ref = VERDICT_PATH
    print(f"[verdict] conclusion={conclusion} -> {verdict_ref}")
    print(json.dumps(stats, indent=2, ensure_ascii=False))


def cmd_status(_args):
    r = kubectl("get", "namespaces", "-o", "name", check=False).stdout
    labs = [l.strip() for l in r.splitlines() if "argocd-lab" in l]
    print("lab namespaces: " + (", ".join(labs) if labs else "無し (down 済み or 未構築)"))
    try:
        series = read_series()
    except SystemExit as e:
        print(e)
        return
    for k, v in sorted(series.items()):
        st = series_stats(v)
        print(f"{k}: n={st['samples']} last={v[-1][1]:.0f}Mi max={st['max_mib']}Mi "
              f"slope={st['slope_mib_per_h']}Mi/h restarts {st['restart_start']}->{st['restart_end']}")


def cmd_down(_args):
    for s in SYSTEMS:
        kubectl("delete", "namespace", s["ns"], "--ignore-not-found=true", "--wait=true")
    leftover = kubectl("get", "namespaces", "-o", "name", check=False).stdout
    residue = [l.strip() for l in leftover.splitlines()
               if "argocd-lab" in l or "p0196" in l]
    if residue:
        raise SystemExit(f"残置あり: {residue}")
    print("[down] lab namespace 2 個削除、残置ゼロ")
    print("[note] CRD (applications.argoproj.io 等) は本番共有のため残す — 削除禁止")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", dest="fn", action="store_const", const=cmd_plan,
                   help="計画を出力 (クラスタ・ネットワーク非接触)")
    g.add_argument("command", nargs="?", choices=("up", "sample", "status", "verdict", "down"),
                   help="up: lab 構築 / sample: 1 回計測 / status: 要約 / verdict: 判定 / down: 削除")
    ap.add_argument("--note", default="", help="sample に付ける注記")
    args = ap.parse_args()
    if args.fn is not None:
        args.fn(args)
        return
    {"up": cmd_up, "sample": cmd_sample, "status": cmd_status,
     "verdict": cmd_verdict, "down": cmd_down}[args.command](args)


if __name__ == "__main__":
    main()

"""「実データを持つ PVC を宣言しているアプリには restic backup CronJob がある」を機械で守る (P-0047)。

なぜ要るか: syncthing-data (20Gi) は 2026-08-10 時点で backup が 1 本も無かった。原因は
syncthing 単体の見落としではなく **手順の穴** で、バックアップ対象の棚卸し (T-0065) は
2026-08-05、syncthing の新設は 2026-08-06 — 棚卸しの後に増えたものは誰も拾わない。
人間の棚卸しは「やった時点」でしか効かないので、次に新しいアプリを足した人が忘れたら
CI が落ちる形にしてここで置き換える。

検査するもの:
  1. app ディレクトリが PVC を宣言しているなら、同じディレクトリに restic を使う CronJob の
     manifest がある
  2. **その manifest が同ディレクトリの kustomization.yaml の resources に載っている**
     — ファイルを置いただけでは ArgoCD は同期しない。配線し忘れも「無い」と同じ
  3. 免除エントリが指す PVC が repo に実在する — 免除リストが現実と切れたまま残るのは、
     ここで潰そうとしている穴と同じ形なので、腐ったら落とす

**既知の死角** (静的スキャンでは映らない。埋められないので伏せずに書き残す):
  - helm chart がレンダリングする PVC (例: immich chart が作る valkey の PVC)。
    CI の ops job は ubuntu-latest + python3 だけで回り、kustomize も helm も入っていない
    (.github/workflows/ci.yml の ops job)。ここを埋めるには CI に helm/kustomize が要る
  - Terraform / コントローラが動的に作る PVC (例: coder-<workspace-id>-home)。
    manifest がリポジトリに存在しないので、この検査には最初から映らない
  この 2 つは次の棚卸しへの引き継ぎ事項。「テストが通った = 全部守られている」ではない。

判定はファイル走査 (collect_apps) と純関数 (find_violations) に分けてある。実リポジトリ
だけを見るテストは「今たまたま通っている」と「正しい」を区別できないので、純関数側は
合成した入力で両方向 (落ちること / 通ること) を固定する。

リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
APPS_DIR = ROOT / "apps"

# 免除。理由付きでしか足せない形にする（理由の書けない免除は、ただの見落としの追認）。
# キーは (app ディレクトリ名, PVC 名)。ここに書いた PVC が repo から消えたらテストは落ちる。
EXEMPT_PVCS = {
    ("autopilot", "autopilot-data"): (
        "autopilot 自身の作業領域。消えて失われるのは思考記録と実行中 Job の中間状態だけで、"
        "リポジトリの状態から作り直せる (apps/autopilot/pvc.yaml が自らそう宣言している)"
    ),
    ("immich", "immich-postgres-data"): (
        "immich 内蔵の日次 DB ダンプが immich-library PVC 配下に出力され、そちらが "
        "immich-restic-backup の対象。DB 本体を別途取る必要が無い (docs/backup.md に明記)"
    ),
}


def _docs(path: Path):
    """1 ファイル内の全 YAML ドキュメントを返す。空ドキュメントと非 mapping は捨てる。"""
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict):
            yield doc


def _kustomization_resources(app_dir: Path) -> list:
    """kustomization.yaml の resources を、比較しやすいよう "./" を落として返す。"""
    path = app_dir / "kustomization.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [str(r).lstrip("./") for r in (data.get("resources") or [])]


def collect_apps(apps_dir: Path) -> dict:
    """apps/ を静的に走査して {app 名: {pvcs, backup_manifests, kustomization_resources}} を作る。

    backup_manifests は「restic という語を含む CronJob を持つ manifest のファイル名」。
    image 名で判定しない — coder の workspace-home backup のように、CronJob 本体は別の
    イメージで restic コンテナを起こす形もあるため (apps/coder/workspace-home-backup-cronjob.yaml)。
    """
    apps = {}
    for app_dir in sorted(p for p in apps_dir.iterdir() if p.is_dir()):
        info = {"pvcs": [], "backup_manifests": [],
                "kustomization_resources": _kustomization_resources(app_dir)}
        for path in sorted(app_dir.rglob("*.yaml")):
            # ベンダリングした helm chart は対象外 (自前で管理している manifest だけを見る)
            if "/charts/" in str(path.relative_to(apps_dir)).replace("\\", "/"):
                continue
            text = path.read_text()
            has_restic_cronjob = False
            for doc in _docs(path):
                kind = doc.get("kind")
                if kind == "PersistentVolumeClaim":
                    name = (doc.get("metadata") or {}).get("name")
                    if name:
                        info["pvcs"].append(name)
                elif kind == "CronJob" and "restic" in text.lower():
                    has_restic_cronjob = True
            if has_restic_cronjob:
                info["backup_manifests"].append(path.name)
        apps[app_dir.name] = info
    return apps


def find_violations(apps: dict, exempt: dict) -> list:
    """純関数。collect_apps 相当の dict と免除表から、違反の説明文リストを返す。"""
    problems = []

    for (app, pvc), reason in sorted(exempt.items()):
        if pvc not in apps.get(app, {}).get("pvcs", []):
            problems.append(
                f"免除エントリ {app}/{pvc} が指す PVC が repo に存在しない。"
                f"PVC を消したなら EXEMPT_PVCS からも消す (免除の理由: {reason})"
            )

    for app, info in sorted(apps.items()):
        resources = info.get("kustomization_resources", [])
        wired = [f for f in info.get("backup_manifests", []) if f in resources]
        for pvc in sorted(info.get("pvcs", [])):
            if (app, pvc) in exempt:
                continue
            if not info.get("backup_manifests"):
                problems.append(
                    f"apps/{app}/ は PVC '{pvc}' を宣言しているのに restic backup CronJob が無い。"
                    f"既存の apps/immich/restic-backup-cronjob.yaml を写して足すか、"
                    f"バックアップ不要なら理由を書いて EXEMPT_PVCS に登録する"
                )
            elif not wired:
                problems.append(
                    f"apps/{app}/ の backup manifest {info['backup_manifests']} が "
                    f"kustomization.yaml の resources に載っていない (PVC '{pvc}')。"
                    f"置いただけでは ArgoCD は同期しないので、取れていないのと同じ"
                )
    return problems


class TestRealRepo(unittest.TestCase):
    def test_every_pvc_has_a_wired_backup(self):
        violations = find_violations(collect_apps(APPS_DIR), EXEMPT_PVCS)
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_scan_actually_sees_something(self):
        """走査そのものが壊れて空を返すと、上のテストは黙って通ってしまう。"""
        apps = collect_apps(APPS_DIR)
        all_pvcs = [f"{a}/{p}" for a, i in apps.items() for p in i["pvcs"]]
        self.assertIn("syncthing/syncthing-data", all_pvcs)
        self.assertIn("restic-backup-cronjob.yaml", apps["syncthing"]["backup_manifests"])


class TestFindViolations(unittest.TestCase):
    """判定の両方向を合成入力で固定する (実 repo だけでは「たまたま通っている」を検出できない)。"""

    COVERED = {"myapp": {"pvcs": ["myapp-data"],
                         "backup_manifests": ["restic-backup-cronjob.yaml"],
                         "kustomization_resources": ["pvc.yaml", "restic-backup-cronjob.yaml"]}}

    def test_covered_app_passes(self):
        self.assertEqual(find_violations(self.COVERED, {}), [])

    def test_pvc_without_backup_fails(self):
        apps = {"myapp": {"pvcs": ["myapp-data"], "backup_manifests": [],
                          "kustomization_resources": ["pvc.yaml"]}}
        violations = find_violations(apps, {})
        self.assertEqual(len(violations), 1)
        self.assertIn("myapp-data", violations[0])

    def test_backup_not_wired_into_kustomization_fails(self):
        """2026-08-10 の verify #2 が見張っている失敗形: ファイルはあるが resources に無い。"""
        apps = {"myapp": {"pvcs": ["myapp-data"],
                          "backup_manifests": ["restic-backup-cronjob.yaml"],
                          "kustomization_resources": ["pvc.yaml"]}}
        violations = find_violations(apps, {})
        self.assertEqual(len(violations), 1)
        self.assertIn("kustomization.yaml", violations[0])

    def test_exempt_pvc_is_skipped(self):
        apps = {"myapp": {"pvcs": ["myapp-data"], "backup_manifests": [],
                          "kustomization_resources": []}}
        self.assertEqual(find_violations(apps, {("myapp", "myapp-data"): "使い捨て"}), [])

    def test_stale_exemption_fails(self):
        """免除が現実と切れたら落とす。放置された免除は、棚卸し漏れと同じ穴。"""
        violations = find_violations(self.COVERED, {("myapp", "gone-data"): "昔あった"})
        self.assertEqual(len(violations), 1)
        self.assertIn("gone-data", violations[0])

    def test_app_without_pvc_is_ignored(self):
        apps = {"stateless": {"pvcs": [], "backup_manifests": [],
                              "kustomization_resources": []}}
        self.assertEqual(find_violations(apps, {}), [])


if __name__ == "__main__":
    unittest.main()

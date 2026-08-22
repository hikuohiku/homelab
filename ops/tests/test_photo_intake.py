"""photo-intake 機構の機械検査 (P-0085)。

なぜ要るか: 人間が sync フォルダ (syncthing-data PVC 上の photo-intake/) に放り込んだ
画像・動画を immich CLI がサーバへ upload し、成功分だけ done/ へ移す CronJob を
apps/syncthing 配下に新設した。この機構は「参照する Secret 名」「immich server の
Service 名・ポート」「PVC のマウント先」が複数ファイルにまたがって一致する必要があり、
どこか 1 箇所がずれると CronJob は CreateContainerConfigError / アップロード失敗と
いう形でしか壊れないうえ、schedule まで待たないと気付けない。manifest を置くだけでは
ArgoCD は同期しない (配線忘れ = P-0047 で実際に踏んだ穴) ので、配線もここで見張る。

検査するもの:
  1. apps/syncthing/photo-intake-cronjob.yaml に CronJob がある
  2. **両 manifest が kustomization.yaml の resources に載っている**
     — ファイルを置いただけでは同期されない
  3. ExternalSecret (Doppler キー IMMICH_API_KEY → Secret
     syncthing-photo-intake-credentials) と、CronJob 側 secretKeyRef の一致。
     ずれると apply 直後に CreateContainerConfigError
  4. 重複取り込みの二重防止がスクリプトに残っていること (done/ への prune + 成功分の mv)。
     サーバ側 checksum 重複排除だけに頼ると「CLI が失敗扱いしたのにローカルには残留」で
     毎回再 upload が走る形になり、防げていることが目視でしか確認できなくなる
  5. substrate 制約: memory limits を付けない / PVC は書き込み可能マウント /
     uid 1000 (syncthing 公式イメージ PUID/PGID=1000) での実行 — done/ への mv は
     intake 配下の所有者と同じ uid でないと失敗しうる

**既知の死角** (CI では映らない。伏せずに書き残す):
  - コンテナ内の sh スクリプトそのものは実行していない。CI の ops job は docker も
    immich サーバも持たないため、find 式や CLI 実行の正しさは
    e2e-proof.json (実クラスタでの end-to-end 通過) でしか証明できない。ここは
    「機構の接続と不変条件」を見る検査
  - Doppler 側にキーが登録済みか、immich server が本当に動いているかは repo 静的検査の
    外。ExternalSecret は manifest 先行 (T-0049 型) なので登録前には Degraded を示す

判定は走査 (scan_intake) と純関数 (find_violations) に分けてある。実リポジトリだけを
見るテストは「今たまたま通っている」と「正しい」を区別できないので、純関数側は合成した
入力で両方向 (落ちること / 通ること) を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_photo_intake`
(CI は `python3 -m unittest discover -s ops/tests -t .` で自動拾いする)。
"""

import unittest
from pathlib import Path

import yaml

from ops import check_credential_map as ccm

ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = ROOT / "apps" / "syncthing"
CRONJOB_FILE = "photo-intake-cronjob.yaml"
EXTERNALSECRET_FILE = "photo-intake-external-secret.yaml"

NAMESPACE = "syncthing"
CRONJOB_NAME = "photo-intake"
VOLUME_NAME = "syncthing-data"
INTAKE_MOUNT = "/var/syncthing"
SECRET_NAME = "syncthing-photo-intake-credentials"
DOPPLER_KEY = "IMMICH_API_KEY"
IMAGE_PREFIX = "ghcr.io/immich-app/immich-cli:"
SERVER_HOST = "immich-server.immich.svc.cluster.local"


def _docs(path: Path):
    for doc in yaml.safe_load_all(path.read_text()):
        if isinstance(doc, dict):
            yield doc


def _kustomization_resources(app_dir: Path) -> list:
    path = app_dir / "kustomization.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [str(r).lstrip("./") for r in (data.get("resources") or [])]


def scan_intake(apps_dir: Path = APP_DIR) -> dict:
    """apps/<app> から intake 機構のサマリを作る。無い項目は None / 空リスト。

    宣言済み鍵・Secret 名は check_credential_map.py の地図から借りてくる
    (「CronJob が参照する鍵は地図にも載っていること」を同じデータ源で判定する)。
    """
    cronjob = None
    externalsecret = None
    cj_path = apps_dir / CRONJOB_FILE
    if cj_path.exists():
        for doc in _docs(cj_path):
            if doc.get("kind") == "CronJob":
                cronjob = doc
                break
    es_path = apps_dir / EXTERNALSECRET_FILE
    if es_path.exists():
        for doc in _docs(es_path):
            if doc.get("kind") == "ExternalSecret":
                externalsecret = doc
                break
    return {
        "cronjob": cronjob,
        "externalsecret": externalsecret,
        "kustomization_resources": _kustomization_resources(apps_dir),
        "declared_doppler_keys": set(ccm.DECLARED_DOPPLER_KEYS),
        "declared_secret_targets": set(ccm.DECLARED_SECRET_TARGETS),
    }


# --- find_violations 用のアクセサ。欠損していても例外ではなく「違反」として落とす ---

def _pod_spec(cronjob: dict) -> dict:
    spec = cronjob.get("spec") or {}
    template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    return template.get("spec") or {}


def _container(cronjob: dict) -> dict:
    containers = _pod_spec(cronjob).get("containers") or []
    return containers[0] if containers else {}


def _env(container: dict, name: str):
    for entry in container.get("env") or []:
        if entry.get("name") == name:
            return entry
    return None


def _script(container: dict) -> str:
    cmd = container.get("command") or []
    for i, part in enumerate(cmd):
        if part == "-c" and i + 1 < len(cmd):
            return str(cmd[i + 1])
    return ""


def find_violations(scan: dict) -> list:
    """純関数。scan_intake 相当の dict から、違反の説明文リストを返す。"""
    problems = []
    cronjob = scan.get("cronjob")
    es = scan.get("externalsecret")
    resources = scan.get("kustomization_resources") or []

    # --- 存在と配線 (P-0047 の教訓: 置いただけでは ArgoCD は同期しない) ---
    if cronjob is None:
        problems.append(
            f"apps/{NAMESPACE}/{CRONJOB_FILE} に CronJob が無い。intake 機構ごと消えたか、"
            "kind/name が想定外"
        )
    if es is None:
        problems.append(
            f"apps/{NAMESPACE}/{EXTERNALSECRET_FILE} に ExternalSecret が無い。"
            f"CronJob が参照する {SECRET_NAME} の作り手が存在しなくなる"
        )
    for f in (CRONJOB_FILE, EXTERNALSECRET_FILE):
        if f not in resources:
            problems.append(
                f"apps/{NAMESPACE}/{f} が kustomization.yaml の resources に載っていない。"
                "置いただけでは ArgoCD は同期しないので、取れていないのと同じ"
            )
    if cronjob is None or es is None:
        return problems

    # --- CronJob の基本形 ---
    meta = cronjob.get("metadata") or {}
    if meta.get("name") != CRONJOB_NAME or meta.get("namespace") != NAMESPACE:
        problems.append(
            f"CronJob の metadata が ({NAMESPACE}, {CRONJOB_NAME}) でない "
            f"(actual: ({meta.get('namespace')}, {meta.get('name')}))"
        )
    spec = cronjob.get("spec") or {}
    schedule = spec.get("schedule")
    if not isinstance(schedule, str) or not schedule.strip():
        problems.append("CronJob の schedule が空。放り込んでからの反映遅延が制御できなくなる")
    if spec.get("concurrencyPolicy") != "Forbid":
        problems.append(
            "CronJob の concurrencyPolicy が Forbid でない。前回 run の残業中に次が起きて "
            "done/ 移動と競合しうる (PROJECT.md 方針 5)"
        )

    container = _container(cronjob)

    # --- イメージ pin (:latest は使わない — repo 流儀) ---
    image = str(container.get("image") or "")
    if not image.startswith(IMAGE_PREFIX):
        problems.append(
            f"image が {IMAGE_PREFIX}<tag> 形でない (actual: {image or '未設定'})。"
            "公式 CLI イメージの pin を外すと entrypoint パスの前提も同時に崩れる"
        )
    elif image[len(IMAGE_PREFIX):] in ("", "latest"):
        problems.append(f"image tag が pin されていない ({image}):latest は使わない")

    # --- credential 参照 (ExternalSecret との一致) ---
    api_key_env = _env(container, DOPPLER_KEY)
    skr = ((api_key_env or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
    if skr.get("name") != SECRET_NAME or skr.get("key") != DOPPLER_KEY:
        problems.append(
            f"env {DOPPLER_KEY} が secretKeyRef ({SECRET_NAME}, {DOPPLER_KEY}) を指していない "
            f"(actual: ({skr.get('name')}, {skr.get('key')}))。"
            "ExternalSecret が作る Secret 名・鍵名とずれると apply 直後に "
            "CreateContainerConfigError になる"
        )
    url_env = _env(container, "IMMICH_INSTANCE_URL") or {}
    url = str(url_env.get("value") or "")
    if SERVER_HOST not in url or not url.rstrip("/").endswith("/api"):
        problems.append(
            f"env IMMICH_INSTANCE_URL が http://{SERVER_HOST}:<port>/api 形でない "
            f"(actual: {url or '未設定'})。immich CLI は <server>/api を要求する"
        )

    # --- PVC マウント (書き込み可能であること。done/ への mkdir/mv が目的) ---
    pod = _pod_spec(cronjob)
    volumes = {v.get("name"): v for v in pod.get("volumes") or []}
    volume = volumes.get(VOLUME_NAME)
    claim = (((volume or {}).get("persistentVolumeClaim")) or {}).get("claimName")
    if claim != VOLUME_NAME:
        problems.append(
            f"volumes.{VOLUME_NAME} が PVC '{VOLUME_NAME}' を指していない。"
            "intake フォルダの置き場所は syncthing-data 上に固定 (PROJECT.md 方針 1)"
        )
    mounts = {m.get("name"): m for m in container.get("volumeMounts") or []}
    mount = mounts.get(VOLUME_NAME)
    if mount is None:
        problems.append(f"コンテナが volume '{VOLUME_NAME}' を mount していない")
    else:
        if mount.get("mountPath") != INTAKE_MOUNT:
            problems.append(
                f"volumeMount の mountPath が {INTAKE_MOUNT} でない "
                f"(actual: {mount.get('mountPath')})"
            )
        if mount.get("readOnly"):
            problems.append(
                "volumeMount が readOnly。done/ への移動・mkdir ができず "
                "取り込み済みファイルが毎回再 upload される"
            )
    pvc_ref = (((volume or {}).get("persistentVolumeClaim")) or {})
    if pvc_ref.get("readOnly"):
        problems.append(
            f"volumes.{VOLUME_NAME} の PVC 参照が readOnly。mount 側より強く "
            "書き込みを禁止し、done/ 移動が必ず失敗する"
        )

    # --- 実行ユーザ (syncthing 公式イメージ PUID/PGID=1000 との一致) ---
    run_as = ((container.get("securityContext") or {}).get("runAsUser"))
    if run_as != 1000:
        problems.append(
            f"securityContext.runAsUser が 1000 でない (actual: {run_as})。"
            "intake 配下は PUID=1000 の syncthing Pod が所有するため、別 uid では "
            "done/ への mv が失敗しうる。変えるなら syncthing 側 PUID と一緒に"
        )

    # --- resources (substrate.md: memory limits は実測なしに付けない) ---
    res = container.get("resources") or {}
    if (res.get("limits") or {}).get("memory") is not None:
        problems.append(
            "resources.limits.memory を付けている。substrate.md 方針 "
            "(memory limits は実測なしに付けない。OOMKill は回復しない)"
        )
    if not (res.get("requests") or {}):
        problems.append("resources.requests が空。node01 単一ノードの予約が何も効いていない")

    # --- 重複防止ロジックの痕跡 (done/ prune + 成功分の mv) ---
    script = _script(container)
    for marker in ("-prune", "$DONE", "upload"):
        if marker not in script:
            problems.append(
                f"起動スクリプトに '{marker}' が無い。重複取り込みの二重防止 "
                "(done/ の prune と成功分の mv) が壊れている恐れ (PROJECT.md 方針 2)"
            )

    # --- ExternalSecret 側 ---
    esspec = es.get("spec") or {}
    store = esspec.get("secretStoreRef") or {}
    if store.get("name") != "doppler" or store.get("kind") != "ClusterSecretStore":
        problems.append(
            "ExternalSecret の secretStoreRef が ClusterSecretStore 'doppler' でない。"
            "他 store への出し替えは check_credential_map.py の前提も同時に崩す"
        )
    target_name = (esspec.get("target") or {}).get("name")
    if target_name != SECRET_NAME:
        problems.append(
            f"ExternalSecret の target.name が {SECRET_NAME} でない "
            f"(actual: {target_name})。CronJob 側 secretKeyRef と必ずずれる"
        )
    entries = esspec.get("data") or []
    mapped = any(
        e.get("secretKey") == DOPPLER_KEY and (e.get("remoteRef") or {}).get("key") == DOPPLER_KEY
        for e in entries
    )
    if not mapped:
        problems.append(
            f"ExternalSecret の data に secretKey={DOPPLER_KEY} ← remoteRef.key="
            f"{DOPPLER_KEY} の対応が無い。T-0049 型の「キー名決め打ち」契約が崩れる"
        )

    # --- 地図 (check_credential_map.py) との一致 ---
    if DOPPLER_KEY not in scan.get("declared_doppler_keys", set()):
        problems.append(
            f"Doppler キー {DOPPLER_KEY} が check_credential_map.py の "
            "DECLARED_DOPPLER_KEYS に無い。参照と宣言をセットで足すこと"
        )
    if SECRET_NAME not in scan.get("declared_secret_targets", set()):
        problems.append(
            f"Secret {SECRET_NAME} が check_credential_map.py の "
            "DECLARED_SECRET_TARGETS に無い。地図が黙って現実とずれる"
        )

    return problems


# ---------------------------------------------------------------------------
# 合成 fixture。「整合している状態」を土台に、各テストが 1 箇所だけ壊す。
# ---------------------------------------------------------------------------

SCRIPT_OK = (
    'set -eu\n'
    'mkdir -p "$DONE"\n'
    'find "$INTAKE" -path "$DONE" -prune -o -type f -print\n'
    '"$CLI" upload "$f" && mv "$f" "$DONE/$rel"\n'
)


def good_cronjob() -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": CRONJOB_NAME, "namespace": NAMESPACE},
        "spec": {
            "schedule": "*/10 * * * *",
            "concurrencyPolicy": "Forbid",
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "photo-intake",
                                    "image": IMAGE_PREFIX + "3.1.0",
                                    "command": ["sh", "-c", SCRIPT_OK],
                                    "env": [
                                        {
                                            "name": "IMMICH_INSTANCE_URL",
                                            "value": f"http://{SERVER_HOST}:2283/api",
                                        },
                                        {
                                            "name": DOPPLER_KEY,
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "name": SECRET_NAME,
                                                    "key": DOPPLER_KEY,
                                                }
                                            },
                                        },
                                    ],
                                    "resources": {"requests": {"cpu": "50m"}},
                                    "securityContext": {"runAsUser": 1000},
                                    "volumeMounts": [
                                        {"name": VOLUME_NAME, "mountPath": INTAKE_MOUNT}
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": VOLUME_NAME,
                                    "persistentVolumeClaim": {"claimName": VOLUME_NAME},
                                }
                            ],
                        }
                    }
                }
            },
        },
    }


def good_externalsecret() -> dict:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": "doppler"},
            "target": {"name": SECRET_NAME},
            "data": [
                {"secretKey": DOPPLER_KEY, "remoteRef": {"key": DOPPLER_KEY}}
            ],
        },
    }


def good_scan() -> dict:
    return {
        "cronjob": good_cronjob(),
        "externalsecret": good_externalsecret(),
        "kustomization_resources": [CRONJOB_FILE, EXTERNALSECRET_FILE],
        "declared_doppler_keys": {DOPPLER_KEY},
        "declared_secret_targets": {SECRET_NAME},
    }


def broken(mutate) -> list:
    """good_scan を mutate して壊し、find_violations に掛ける。"""
    scan = good_scan()
    mutate(scan)
    return find_violations(scan)


class TestRealRepo(unittest.TestCase):
    def test_real_repo_has_no_violations(self):
        violations = find_violations(scan_intake())
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_scan_actually_sees_something(self):
        """走査が壊れて空を返すと、上のテストは黙って通ってしまう。"""
        scan = scan_intake()
        self.assertIsNotNone(scan["cronjob"])
        self.assertIsNotNone(scan["externalsecret"])
        self.assertIn(CRONJOB_FILE, scan["kustomization_resources"])
        self.assertIn(DOPPLER_KEY, scan["declared_doppler_keys"])
        container = _container(scan["cronjob"])
        self.assertTrue(_script(container), "command sh -c のスクリプトが抽出できない")


class TestFindViolations(unittest.TestCase):
    """判定の両方向を合成入力で固定する (実 repo だけでは「たまたま通っている」を検出できない)。"""

    def test_consistent_setup_passes(self):
        self.assertEqual(find_violations(good_scan()), [])

    def test_missing_cronjob_fails(self):
        violations = broken(lambda s: s.update(cronjob=None))
        self.assertEqual(len(violations), 1)
        self.assertIn(CRONJOB_FILE, violations[0])

    def test_missing_externalsecret_fails(self):
        violations = broken(lambda s: s.update(externalsecret=None))
        self.assertEqual(len(violations), 1)
        self.assertIn(SECRET_NAME, violations[0])

    def test_cronjob_not_wired_into_kustomization_fails(self):
        """見張っている既知の失敗形: ファイルはあるが resources に無い。"""
        violations = broken(lambda s: s["kustomization_resources"].remove(CRONJOB_FILE))
        self.assertEqual(len(violations), 1)
        self.assertIn("kustomization.yaml", violations[0])
        self.assertIn(CRONJOB_FILE, violations[0])

    def test_wrong_secret_name_in_cronjob_fails(self):
        """apply 直後に CreateContainerConfigError になる形。"""

        def twist(s):
            c = _container(s["cronjob"])
            c["env"][1]["valueFrom"]["secretKeyRef"]["name"] = "typo-credentials"

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("CreateContainerConfigError", violations[0])
        self.assertIn("typo-credentials", violations[0])

    def test_unpinned_image_tag_fails(self):

        def twist(s):
            _container(s["cronjob"])["image"] = IMAGE_PREFIX + "latest"

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("latest", violations[0])

    def test_memory_limit_fails(self):
        """substrate.md: memory limits は実測なしに付けない (OOMKill は回復しない)。"""

        def twist(s):
            c = _container(s["cronjob"])
            c["resources"] = {"requests": {"memory": "128Mi"}, "limits": {"memory": "256Mi"}}

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("limits.memory", violations[0])

    def test_readonly_mount_fails(self):
        """readOnly だと done/ への mv ができず毎回再 upload になる。"""

        def twist(s):
            c = _container(s["cronjob"])
            c["volumeMounts"][0]["readOnly"] = True

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("readOnly", violations[0])

    def test_run_as_user_mismatch_fails(self):
        """uid がずれると syncthing 所有のファイルの mv が失敗しうる。"""

        def twist(s):
            _container(s["cronjob"])["securityContext"]["runAsUser"] = 0

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("runAsUser", violations[0])

    def test_missing_concurrency_forbid_fails(self):

        def twist(s):
            del s["cronjob"]["spec"]["concurrencyPolicy"]

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn("Forbid", violations[0])

    def test_script_without_done_move_fails(self):
        """upload だけで done/ へ動かさない機構は重複防止の半分が欠ける。"""

        def twist(s):
            _container(s["cronjob"])[
                "command"
            ] = ["sh", "-c", 'find "$INTAKE" -type f -exec "$CLI" upload {} \;\n']

        violations = broken(twist)
        self.assertGreaterEqual(len(violations), 1)
        self.assertIn("$DONE", "\n".join(violations))

    def test_externalsecret_target_mismatch_fails(self):

        def twist(s):
            s["externalsecret"]["spec"]["target"]["name"] = "another-secret"

        violations = broken(twist)
        self.assertEqual(len(violations), 1)
        self.assertIn(SECRET_NAME, violations[0])

    def test_stale_doppler_declaration_fails(self):
        """宣言が先に消えると地図が現実と切れる。腐った宣言は落とす。"""
        violations = broken(lambda s: s["declared_doppler_keys"].clear())
        self.assertEqual(len(violations), 1)
        self.assertIn(DOPPLER_KEY, violations[0])


if __name__ == "__main__":
    unittest.main()

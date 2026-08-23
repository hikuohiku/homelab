"""apps/adguard/ の manifest 契約を固定する (P-0270)。

リポジトリルートから `python3 -m unittest ops.tests.test_adguard_manifest`。

CI の ops job に kubectl/kustomize は入っていない (.github/workflows/ci.yml 実測、
test_backup_coverage.py の冒頭にも同じ事情が書いてある) ので、verify #1 の
`kubectl kustomize` はここでは再現しない。代わりに PyYAML で実ファイルをパースし、
「kustomization.yaml の resources に載ったファイル群を連結したもの」= レンダリング結果
として静的に再構成して検査する。ネットワークには一切出ない
(test_syncthing_acceptance.py 流儀)。

固定する契約と理由:
  - イメージ pin は deployment 内 2 箇所 (seed-config initContainer + 本体) で必ず同値、
    かつ inventory.json の current とも一致 — 片だけ上げる部分更新 (#49 型の静放置への
    戻り道) をここで落とす。バージョンそのものの anchor は置かない: 正しい手順での
    引き上げ (deployment 2 箇所 + inventory current を同一 PR で) なら触らずに通る
  - memory limits は付けない (substrate 規則)。CPU limits のみ
  - Service は tailscale loadBalancerClass + hostname annotation。管理 UI の外部公開は
    Service 型の選択で担保 (PROJECT.md やらないこと)
  - backup は append-only 鍵 / retention のみ削除鍵 (T-0106 / P-0028 型)
  - PVC に Prune=false — PROJECT.md ロールバック節の「prune で PVC も消える」を回避する
  - seed ConfigMap は「初回だけコピー」であって conf への直接 mount ではない
    (直接 mount すると read-only になり UI からの設定保存が壊れる)
"""

import json
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "adguard"

# kustomization.yaml の resources に載るべき全ファイル。この 6 つの連結が
# ArgoCD が同期する実体。減っても増えても落ちる (意図的な変更はここも直す)
RESOURCE_FILES = [
    "pvc.yaml",
    "configmap.yaml",
    "deployment.yaml",
    "service-tailnet.yaml",
    "restic-external-secret.yaml",
    "restic-backup-cronjob.yaml",
]

EXPECTED_OBJECTS = {
    ("PersistentVolumeClaim", "adguard-data"),
    ("ConfigMap", "adguard-seed-config"),
    ("Deployment", "adguard"),
    ("Service", "adguard"),
    ("ExternalSecret", "adguard-restic-credentials"),
    ("ExternalSecret", "adguard-restic-backup-credentials"),
    ("CronJob", "adguard-restic-backup"),
    ("CronJob", "adguard-restic-retention"),
}


def load_docs(name):
    """1 ファイル内の全 YAML ドキュメントを返す (空ドキュメントと非 mapping は捨てる)。"""
    return [d for d in yaml.safe_load_all((APP_DIR / name).read_text(encoding="utf-8"))
            if isinstance(d, dict)]


def rendered_objects():
    """resources 全連結 = レンダリング相当。(doc, 出元ファイル名) のリスト。"""
    out = []
    for name in RESOURCE_FILES:
        out.extend((doc, name) for doc in load_docs(name))
    return out


def pick(kind, name):
    hits = [d for d, _ in rendered_objects()
            if d.get("kind") == kind
            and (d.get("metadata") or {}).get("name") == name]
    assert len(hits) == 1, f"kind={kind} name={name} が一意に見つからない ({len(hits)} 件)"
    return hits[0]


def pod_containers(pod_spec):
    """initContainers + containers を平たく返す (memory limits 検査の共通歩き方)。"""
    return list(pod_spec.get("initContainers") or []) + list(pod_spec.get("containers") or [])


def containers_with_memory_limits(pod_spec):
    return [c["name"] for c in pod_containers(pod_spec)
            if "memory" in ((c.get("resources") or {}).get("limits") or {})]


class TestRenderedContract(unittest.TestCase):
    """resources への配線とレンダリング全体の形。"""

    def test_kustomization_lists_exactly_the_resource_files(self):
        kust = yaml.safe_load((APP_DIR / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertEqual([str(r).lstrip("./") for r in kust["resources"]],
                         RESOURCE_FILES)

    def test_no_yaml_file_is_left_unwired(self):
        """ディレクトリ内の yaml が kustomization / application を除き全て配線されていること。
        置いただけでは ArgoCD は同期しない (test_backup_coverage.py が潰した穴と同型)。"""
        wired = set(RESOURCE_FILES) | {"kustomization.yaml", "application.yaml"}
        stray = sorted(p.name for p in APP_DIR.glob("*.yaml") if p.name not in wired)
        self.assertEqual(stray, [], f"kustomization に載っていない yaml: {stray}")

    def test_render_is_exactly_the_eight_expected_objects(self):
        got = {(d["kind"], (d.get("metadata") or {}).get("name"))
               for d, _ in rendered_objects()}
        self.assertEqual(got, EXPECTED_OBJECTS)

    def test_namespaced_objects_all_live_in_adguard_namespace(self):
        for doc, fname in rendered_objects():
            if doc["kind"] == "Application":
                continue
            self.assertEqual((doc.get("metadata") or {}).get("namespace"), "adguard",
                             f"{fname} の {doc['kind']}")

    def test_root_app_of_apps_picks_up_the_application(self):
        root = yaml.safe_load(
            (ROOT / "apps" / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertIn("adguard/application.yaml", root["resources"])


class TestApplication(unittest.TestCase):
    """ArgoCD Application 単体の契約。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_docs("application.yaml")[0]
        cls.spec = cls.app["spec"]

    def test_points_at_apps_adguard_with_automated_sync(self):
        self.assertEqual(self.app["metadata"]["name"], "adguard")
        self.assertEqual(self.spec["source"]["path"], "apps/adguard")
        self.assertEqual(self.spec["destination"]["namespace"], "adguard")
        self.assertTrue(self.spec["syncPolicy"]["automated"]["prune"])
        self.assertTrue(self.spec["syncPolicy"]["automated"]["selfHeal"])

    def test_namespace_is_created_by_argocd(self):
        opts = self.spec["syncPolicy"]["syncOptions"]
        self.assertIn("CreateNamespace=true", opts)


class TestDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dep = pick("Deployment", "adguard")
        cls.pod = cls.dep["spec"]["template"]["spec"]
        cls.main = next(c for c in cls.pod["containers"]
                        if c["name"] == "adguardhome")

    def test_single_replica_recreate_strategy(self):
        # 内部 DB を単一 PVC に持つため rolling 中の二重起動を避ける (syncthing 同型)
        self.assertEqual(self.dep["spec"]["replicas"], 1)
        self.assertEqual(self.dep["spec"]["strategy"]["type"], "Recreate")

    def test_no_service_account_token(self):
        self.assertIs(self.pod["automountServiceAccountToken"], False)

    def test_image_is_pinned_identically_in_both_places(self):
        init = self.pod["initContainers"][0]
        self.assertEqual(len(self.pod["initContainers"]), 1)
        self.assertEqual(init["name"], "seed-config")
        self.assertEqual(init["image"], self.main["image"],
                         "seed-config initContainer と本体でタグがずれている。"
                         "inventory の adguard エントリは両者が同値である前提で書かれている")
        repo, _, tag = self.main["image"].partition(":")
        self.assertEqual(repo, "adguard/adguardhome")
        self.assertTrue(tag.startswith("v"),
                        "adguard/adguardhome のタグは v 付き形式のみ (Docker Hub 実測)")

    def test_main_container_disables_self_update(self):
        # コンテナ内自己更新を許すと image pin と version-watcher の網が両方無意味になる
        self.assertIn("--no-check-update", self.main["args"])
        self.assertIn("/opt/adguardhome/conf/AdGuardHome.yaml", self.main["args"])

    def test_seed_is_copied_once_not_mounted_into_conf(self):
        init = self.pod["initContainers"][0]
        cmd = " ".join(init["command"])
        self.assertIn("test -f /conf/AdGuardHome.yaml ||", cmd,
                      "既存設定を上書きしないガードが消えている")
        self.assertIn("cp /seed/AdGuardHome.yaml /conf/AdGuardHome.yaml", cmd)
        # 本体コンテナが seed ConfigMap を mount していないこと
        # (mount すると conf が read-only になり UI からの設定保存が壊れる)
        mounted = {m["name"] for m in self.main.get("volumeMounts") or []}
        self.assertNotIn("seed", mounted)

    def test_data_volume_mounts_split_conf_and_work_by_subpath(self):
        mounts = {(m["name"], m.get("subPath")): m["mountPath"]
                  for m in self.main["volumeMounts"]}
        self.assertEqual(mounts[("data", "conf")], "/opt/adguardhome/conf")
        self.assertEqual(mounts[("data", "work")], "/opt/adguardhome/work")
        vols = {v["name"]: v for v in self.pod["volumes"]}
        claim = vols["data"]["persistentVolumeClaim"]
        self.assertEqual(claim["claimName"], "adguard-data")
        self.assertEqual(vols["seed"]["configMap"]["name"], "adguard-seed-config")

    def test_probes_hit_named_http_port_on_both_phases(self):
        ports = {p["name"]: p["containerPort"] for p in self.main["ports"]}
        self.assertEqual(ports["dns-tcp"], 53)
        self.assertEqual(ports["dns-udp"], 53)
        self.assertEqual(ports["http"], 3000)
        for probe in ("readinessProbe", "livenessProbe"):
            http_get = self.main[probe]["httpGet"]
            self.assertEqual(http_get["path"], "/")
            self.assertEqual(http_get["port"], "http")

    def test_requests_minimal_and_only_cpu_limited(self):
        res = self.main["resources"]
        self.assertIn("cpu", res.get("requests") or {})
        self.assertIn("memory", res.get("requests") or {})
        self.assertIn("cpu", res.get("limits") or {})
        self.assertEqual(containers_with_memory_limits(self.pod), [],
                         "memory limits は付けない (substrate 規則)")

    def test_init_container_has_no_memory_limit(self):
        self.assertEqual(containers_with_memory_limits(self.pod), [])


class TestServiceTailnet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc = pick("Service", "adguard")
        cls.spec = cls.svc["spec"]

    def test_tailnet_load_balancer_with_hostname_annotation(self):
        self.assertEqual(self.spec["type"], "LoadBalancer")
        self.assertEqual(self.spec["loadBalancerClass"], "tailscale")
        self.assertEqual((self.svc["metadata"].get("annotations") or {})
                         .get("tailscale.com/hostname"), "adguard")

    def test_exposes_dns_tcp_udp_and_ui(self):
        ports = {p["name"]: p for p in self.spec["ports"]}
        self.assertEqual(ports["dns-tcp"]["port"], 53)
        self.assertEqual(ports["dns-tcp"]["protocol"], "TCP")
        self.assertEqual(ports["dns-udp"]["port"], 53)
        self.assertEqual(ports["dns-udp"]["protocol"], "UDP")
        self.assertEqual(ports["http"]["port"], 3000)

    def test_selects_the_adguard_pods(self):
        self.assertEqual(self.spec["selector"], {"app": "adguard"})


class TestPVC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pvc = pick("PersistentVolumeClaim", "adguard-data")

    def test_survives_argocd_prune(self):
        # ロールバック (revert PR) 後も人間が育てた設定を残すための指定。
        # 外れると revert 1 本でデータごと消える
        self.assertEqual(
            (self.pvc["metadata"].get("annotations") or {})
            .get("argocd.argoproj.io/sync-options"),
            "Prune=false")

    def test_local_path_5gi_rwo(self):
        spec = self.pvc["spec"]
        self.assertEqual(spec["storageClassName"], "local-path")
        self.assertEqual(spec["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(spec["resources"]["requests"]["storage"], "5Gi")


class TestSeedConfigMap(unittest.TestCase):
    """種の設定。ここが壊れると UI が :3000 から消える / 何もブロックされない。"""

    @classmethod
    def setUpClass(cls):
        cm = pick("ConfigMap", "adguard-seed-config")
        cls.raw = cm["data"]["AdGuardHome.yaml"]
        cls.cfg = yaml.safe_load(cls.raw)

    def test_http_port_fixed_to_3000(self):
        # 固定しないとウィザード完了後に UI が 0.0.0.0:80 へ移り、
        # Service/probe (:3000) から UI が消える
        self.assertEqual(self.cfg["http"]["address"], "0.0.0.0:3000")

    def test_dns_binds_all_interfaces_on_53(self):
        self.assertEqual(self.cfg["dns"]["port"], 53)
        self.assertIn("0.0.0.0", self.cfg["dns"]["bind_hosts"])

    def test_ships_an_enabled_filter_and_upstream(self):
        # 「入れたのに何もブロックされない」を避けるのが種の存在理由の一つ
        self.assertTrue(any(f.get("enabled") for f in self.cfg["filters"]))
        self.assertTrue(self.cfg["dns"]["upstream_dns"])
        # DoH 上流の名前解決のために bootstrap が要る
        self.assertTrue(self.cfg["dns"]["bootstrap_dns"])

    def test_admin_user_is_not_provisioned(self):
        # 管理ユーザー (パスワード) は人間がウィザードで作る。自動化が決めない
        self.assertNotIn("users", self.cfg)


class TestResticCronJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup = pick("CronJob", "adguard-restic-backup")
        cls.retention = pick("CronJob", "adguard-restic-retention")

    def backup_pod(self):
        return self.backup["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    def retention_pod(self):
        return self.retention["spec"]["jobTemplate"]["spec"]["template"]["spec"]

    def test_schedules_outside_existing_backup_and_retention_bands(self):
        # 既存占有: backup 2:45/3:10/3:30/3:40/3:55、retention 日曜午前4時台。
        # 衝突時刻への変更はここで弾く (意図的に変えるならここも直す)
        self.assertEqual(self.backup["spec"]["schedule"], "20 3 * * *")
        self.assertEqual(self.retention["spec"]["schedule"], "10 5 * * 0")
        for cj in (self.backup, self.retention):
            self.assertEqual(cj["spec"]["concurrencyPolicy"], "Forbid")

    def test_jobs_do_not_mount_service_account_and_never_restart(self):
        for pod in (self.backup_pod(), self.retention_pod()):
            self.assertIs(pod["automountServiceAccountToken"], False)
            self.assertEqual(pod["restartPolicy"], "Never")

    def test_same_restic_image_pin_in_both_cronjobs(self):
        b_img = self.backup_pod()["containers"][0]["image"]
        r_img = self.retention_pod()["containers"][0]["image"]
        self.assertEqual(b_img, r_img)
        self.assertTrue(b_img.startswith("restic/restic:"))

    def test_repository_suffix_is_adguard(self):
        for pod in (self.backup_pod(), self.retention_pod()):
            env = {e["name"]: e for e in pod["containers"][0]["env"]}
            self.assertEqual(env["RESTIC_REPOSITORY"]["value"],
                             "b2:$(RESTIC_B2_BUCKET):adguard")

    def test_backup_uses_append_only_key_retention_uses_delete_key(self):
        # T-0106 / P-0028 型: 削除権限つき鍵は retention だけが使う
        def secret_name(pod):
            env = {e["name"]: e for e in pod["containers"][0]["env"]}
            return env["B2_ACCOUNT_KEY"]["valueFrom"]["secretKeyRef"]["name"]

        self.assertEqual(secret_name(self.backup_pod()),
                         "adguard-restic-backup-credentials")
        self.assertEqual(secret_name(self.retention_pod()),
                         "adguard-restic-credentials")

    def test_backup_mounts_are_readonly_and_cover_conf_and_work(self):
        c = self.backup_pod()["containers"][0]
        mounts = {m.get("subPath"): m for m in c["volumeMounts"]}
        self.assertEqual(set(mounts), {"conf", "work"})
        for m in mounts.values():
            self.assertIs(m["readOnly"], True,
                          "backup は読むだけで、書き込みは構造的に禁止されていて然るべき")

    def test_retention_keeps_standard_generations_without_pvc_mount(self):
        # 保持世代は既存アプリと同じ (--keep-daily 7 --keep-weekly 4 --keep-monthly 6)
        cmd = " ".join(self.retention_pod()["containers"][0]["command"])
        self.assertIn("--keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune", cmd)
        pod = self.retention_pod()
        self.assertFalse(pod.get("volumes"),
                         "retention は PVC に触れないためマウント不要")

    def test_security_contexts_are_minimal(self):
        b_sc = self.backup_pod()["containers"][0]["securityContext"]
        # AGH コンテナは root で conf/ を書くため所有権を予測せず読む。syncthing 同型
        self.assertEqual(b_sc["runAsUser"], 0)
        self.assertIs(b_sc["allowPrivilegeEscalation"], False)
        self.assertEqual(b_sc["capabilities"]["drop"], ["ALL"])
        self.assertEqual(b_sc["capabilities"]["add"], ["DAC_READ_SEARCH"])
        r_sc = self.retention_pod()["containers"][0]["securityContext"]
        self.assertIs(r_sc["allowPrivilegeEscalation"], False)
        self.assertEqual(r_sc["capabilities"]["drop"], ["ALL"])
        self.assertNotIn("add", r_sc.get("capabilities") or {})

    def test_no_memory_limits_in_any_job_container(self):
        for pod in (self.backup_pod(), self.retention_pod()):
            self.assertEqual(containers_with_memory_limits(pod), [],
                             "memory limits は付けない (substrate 規則)")


class TestExternalSecrets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = {d["metadata"]["name"]: d
                    for d in load_docs("restic-external-secret.yaml")}

    def test_two_secrets_from_cluster_store_doppler(self):
        self.assertEqual(sorted(self.docs),
                         ["adguard-restic-backup-credentials",
                          "adguard-restic-credentials"])
        for es in self.docs.values():
            self.assertEqual(es["spec"]["secretStoreRef"],
                             {"kind": "ClusterSecretStore", "name": "doppler"})
            self.assertEqual(es["spec"]["target"]["name"],
                             es["metadata"]["name"])
            self.assertEqual(es["spec"]["target"]["deletionPolicy"], "Retain")

    def test_backup_secret_pulls_append_only_b2_keys(self):
        data = {d["secretKey"]: d["remoteRef"]["key"]
                for d in self.docs["adguard-restic-backup-credentials"]["spec"]["data"]}
        self.assertEqual(data["B2_ACCOUNT_ID"], "B2_ACCOUNT_ID_APPEND_ONLY")
        self.assertEqual(data["B2_ACCOUNT_KEY"], "B2_ACCOUNT_KEY_APPEND_ONLY")
        self.assertEqual(data["RESTIC_PASSWORD"], "RESTIC_PASSWORD")

    def test_retention_secret_pulls_delete_capable_b2_keys(self):
        data = {d["secretKey"]: d["remoteRef"]["key"]
                for d in self.docs["adguard-restic-credentials"]["spec"]["data"]}
        self.assertEqual(data["B2_ACCOUNT_ID"], "B2_ACCOUNT_ID")
        self.assertEqual(data["B2_ACCOUNT_KEY"], "B2_ACCOUNT_KEY")


class TestInventorySync(unittest.TestCase):
    """inventory.json ↔ manifest の相互一致。version-watcher の網に入って初めて
    「入れたのに誰も見てない」を防げる (spec DoD 2 の本体)。"""

    @classmethod
    def setUpClass(cls):
        inv = json.loads((ROOT / "ops" / "inventory.json").read_text(encoding="utf-8"))
        cls.targets = {t["id"]: t for t in inv["targets"]}

    def test_required_fields_are_present_and_files_exist(self):
        for tid in ("adguard", "adguard-restic-image"):
            t = self.targets[tid]
            for field in ("id", "kind", "name", "current", "file", "match",
                          "upstream", "policy", "note"):
                self.assertTrue(t.get(field), f"{tid}.{field} が空")
            self.assertTrue((ROOT / t["file"]).is_file(),
                            f"{tid}.file '{t['file']}' が実在しない")
            self.assertTrue(t["upstream"].startswith("github:"))

    def test_match_strings_actually_appear_in_target_files(self):
        # match が実ファイルの表記とずれていると watcher は永遠に何も見つけられない
        for tid in ("adguard", "adguard-restic-image"):
            t = self.targets[tid]
            text = (ROOT / t["file"]).read_text(encoding="utf-8")
            self.assertIn(t["match"], text)

    def test_inventory_current_equals_deployed_image_tags(self):
        dep_image = pick("Deployment", "adguard") \
            ["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(self.targets["adguard"]["current"], dep_image.split(":", 1)[1])
        self.assertEqual(self.targets["adguard"]["file"],
                         "apps/adguard/deployment.yaml")

        cj_image = pick("CronJob", "adguard-restic-backup") \
            ["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(self.targets["adguard-restic-image"]["current"],
                         cj_image.split(":", 1)[1])
        self.assertEqual(self.targets["adguard-restic-image"]["file"],
                         "apps/adguard/restic-backup-cronjob.yaml")


if __name__ == "__main__":
    unittest.main()

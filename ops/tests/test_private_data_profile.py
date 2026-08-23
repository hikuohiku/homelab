"""私的データ分離 Job プロファイル (P-0161) の**構造制約**を機械検査する。

なぜ要るか: ops/profiles/private-data/ の器は「trifecta の 3 要素が同時に揃わない」
ことを YAML の形で保証する規約だが、レビューのたびに人間が行レベルで読み直す運用では
「誰かが便利そうなので env を 1 行足した」「publisher にも private-data を mount
させた」ような破れが静かに通ってしまう。壁は目視ではなく機械で見る。

このテストが見るのは**構造**だけであることに注意:

  - model コンテナの env 非存在・Secret mount の readOnly・NetworkPolicy の
    egress 空・podSelector と Pod ラベルの一致など、「YAML が正しい形をしているか」
  - 挙動の実測 (egress が実際に拒否されるか / 成果物がブランチに着くか) は
    クラスタ上の demo run (ops/projects/logs/P-0161/demo.json) が担う。
    両者は代替ではなく補完。構造が green でも k3s の netpol が効かなければ
    demo.json の `egress_denied` は false になる — それが正しい失敗の記録になる

リポジトリルートから `python3 -m unittest ops.tests.test_private_data_profile`
(CI は discover -s ops/tests -t .)。
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE = ROOT / "ops" / "profiles" / "private-data"

MODEL_LABEL = "p0161-private-data-model"
FIXTURE_SECRET = "p0161-mail-fixture"
PUSH_TOKEN_KEY = "AUTOPILOT_GITHUB_TOKEN"


def load_docs(name):
    """profile 配下の YAML を multi-doc で読む。parse 失敗は即 red (黙らせない)。"""
    with open(PROFILE / name) as f:
        return [d for d in yaml.safe_load_all(f) if d is not None]


def job_docs():
    docs = load_docs("job.yaml")
    by_kind_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    return docs, by_kind_name


def model_job(by_kind_name):
    return by_kind_name[("Job", "p0161-private-data-model")]


def publisher_job(by_kind_name):
    return by_kind_name[("Job", "p0161-private-data-publisher")]


def all_containers(pod_spec):
    """initContainers 込みで列挙する。壁は「待ち側」にも効いている必要がある。"""
    out = list(pod_spec.get("initContainers") or [])
    out.extend(pod_spec.get("containers") or [])
    return out


class TestFilesExist(unittest.TestCase):
    def test_profile_files_exist(self):
        for name in (
            "README.md",
            "job.yaml",
            "networkpolicy.yaml",
            "secret-fixture.yaml",
        ):
            self.assertTrue((PROFILE / name).is_file(), f"{name} が無い")

    def test_job_yaml_has_three_resources(self):
        _, by_kind_name = job_docs()
        self.assertIn(("PersistentVolumeClaim", "p0161-handoff"), by_kind_name)
        self.assertIn(("Job", "p0161-private-data-model"), by_kind_name)
        self.assertIn(("Job", "p0161-private-data-publisher"), by_kind_name)


class TestModelContainerTakesNoCredentials(unittest.TestCase):
    def setUp(self):
        _, by_kind_name = job_docs()
        self.job = model_job(by_kind_name)

    def test_model_containers_have_no_env_at_all(self):
        # dod「API 鍵 env を一切受け取らない」。envFrom も env[valueFrom] も
        # env 自体の存在ごと禁じる — 「鍵以外なら許可」にすると鍵の定義が
        # 薄まっていく滑り台になるため。例外を作るならこのテストと README を同時に変える
        pod_spec = self.job["spec"]["template"]["spec"]
        for c in all_containers(pod_spec):
            with self.subTest(container=c["name"]):
                self.assertNotIn("env", c, f"{c['name']}: env を足さないこと")
                self.assertNotIn("envFrom", c, f"{c['name']}: envFrom を足さないこと")

    def test_model_pod_does_not_automount_service_account(self):
        # API 鍵ゼロでも SA token が automount されれば別の口が生える
        spec = self.job["spec"]["template"]["spec"]
        self.assertIs(spec.get("automountServiceAccountToken"), False)

    def test_jobs_fail_fast_and_expire(self):
        # backoffLimit>0 だと失敗が再試行で隠れ、activeDeadlineSeconds 無しだと
        # hang した probe が永遠に残る。どちらも「赤を見せない」方向の誤り
        _, by_kind_name = job_docs()
        for name in ("p0161-private-data-model", "p0161-private-data-publisher"):
            spec = by_kind_name[("Job", name)]["spec"]
            with self.subTest(job=name):
                self.assertEqual(spec["backoffLimit"], 0)
                self.assertGreater(int(spec["activeDeadlineSeconds"]), 0)

    def test_no_memory_limits_anywhere(self):
        # substrate.md: memory limits は実測の裏付けなしに付けない
        # (OOMKill は回復しない)。CPU limits は throttle なので対象外
        _, by_kind_name = job_docs()
        for name in ("p0161-private-data-model", "p0161-private-data-publisher"):
            spec = by_kind_name[("Job", name)]["spec"]["template"]["spec"]
            for c in all_containers(spec):
                with self.subTest(job=name, container=c["name"]):
                    limits = (c.get("resources") or {}).get("limits") or {}
                    self.assertNotIn(
                        "memory", limits,
                        "memory limits を付けるなら実測 (OOMKill 観察) が先",
                    )


class TestMountSeparation(unittest.TestCase):
    """要素 (A): 私的データに触れるのは model Pod だけ、書き込めないのは readOnly。"""

    def setUp(self):
        _, by_kind_name = job_docs()
        self.model = model_job(by_kind_name)
        self.publisher = publisher_job(by_kind_name)

    def _mount_of(self, pod_spec, mount_path):
        mounts = []
        for c in all_containers(pod_spec):
            for m in c.get("volumeMounts") or []:
                if m["mountPath"] == mount_path:
                    mounts.append((c["name"], m))
        return mounts

    def test_model_reads_private_data_from_secret_readonly(self):
        pod_spec = self.model["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        secret_vol = volumes.get("private-data")
        self.assertIsNotNone(secret_vol, "model Pod に private-data volume が無い")
        self.assertEqual(secret_vol.get("secret", {}).get("secretName"),
                         FIXTURE_SECRET)
        mounts = self._mount_of(pod_spec, "/private-data")
        self.assertEqual(len(mounts), 1)
        cname, m = mounts[0]
        self.assertTrue(m.get("readOnly"),
                        f"{cname}: /private-data は readOnly mount でなければならない")
        # 書けたら fixture を差し替えられる = 「読む手」が汚染源になりうる
        self.assertEqual(cname, "model-session")

    def test_model_handoff_volume_is_the_pvc_bridge(self):
        pod_spec = self.model["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod_spec["volumes"]}
        self.assertEqual(volumes["handoff"].get("persistentVolumeClaim",
                                               {}).get("claimName"),
                         "p0161-handoff")

    def test_publisher_never_references_private_secret(self):
        # 最も強い検査: publisher Job の YAML 全文に secret 名自体が出てこない。
        # volume でも env でも将来追加されるどんな参照経路もこれで落ちる
        blob = yaml.safe_dump(self.publisher)
        self.assertNotIn(
            FIXTURE_SECRET, blob,
            "publisher Job が私的データ Secret を参照している — 要素 (A) の崩壊",
        )

    def test_publisher_mounts_are_readonly_except_its_own_emptydir(self):
        # handoff PVC は成果物の置き場所ではない (push 入力は emptyDir に組む)。
        # publisher 側の書き込み先が emptyDir (/publish) だけであることを固定する
        pod_spec = self.publisher["spec"]["template"]["spec"]
        writable = []
        for c in all_containers(pod_spec):
            for m in c.get("volumeMounts") or []:
                if not m.get("readOnly"):
                    writable.append((c["name"], m["mountPath"]))
        self.assertEqual(writable, [("publisher", "/publish")])

    def test_publisher_holds_push_credential_only_via_secretkeyref(self):
        # 口を持つのが publisher だけである対比を固定する。token は既存の
        # autopilot-credentials Secret (Doppler 同期) を参照するだけで、
        # このプロファイルが新しく credential を作らないことも意味する
        pod_spec = self.publisher["spec"]["template"]["spec"]
        main = pod_spec["containers"][0]
        refs = [
            e for e in (main.get("env") or [])
            if e.get("valueFrom", {}).get("secretKeyRef", {}).get("key")
            == PUSH_TOKEN_KEY
        ]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["valueFrom"]["secretKeyRef"]["name"],
                         "autopilot-credentials")


class TestNetworkPolicy(unittest.TestCase):
    """要素 (C): model Pod の外向き接続を完全拒否する 1 枚。"""

    def setUp(self):
        self.np = load_docs("networkpolicy.yaml")[0]

    def test_egress_is_empty_list(self):
        # egress: [] (空配列) だけが「全拒否」。egress キー自体の省略は
        # 「制限なし」という逆の意味になるので区別して検査する
        self.assertIsNotNone(self.np["spec"].get("egress"))
        self.assertEqual(self.np["spec"]["egress"], [])

    def test_policy_types_is_egress_only(self):
        # Ingress を締めると観測系 (logs 等) への影響の検討範囲が増える。
        # 本プロファイルが締めるのは出ていく方向だけ
        self.assertEqual(self.np["spec"]["policyTypes"], ["Egress"])

    def test_selects_model_pods_and_only_them(self):
        sel = self.np["spec"]["podSelector"]["matchLabels"]
        _, by_kind_name = job_docs()
        template_labels = (
            model_job(by_kind_name)["spec"]["template"]["metadata"]["labels"]
        )
        # 一致が崩れると拒否が効かなくなる (壁の取り付け位置の検査)
        self.assertEqual(sel, template_labels)
        # publisher Pod は選んでいない (選ぶと push が死ぬ = 実験が成立しない)
        pub_labels = (
            publisher_job(by_kind_name)["spec"]["template"]["metadata"]["labels"]
        )
        self.assertNotEqual(sel, pub_labels)
        np_blob = yaml.safe_dump(self.np)
        self.assertNotIn("p0161-private-data-publisher", np_blob)

    def test_same_namespace_as_the_jobs(self):
        _, by_kind_name = job_docs()
        ns = by_kind_name[("Job", "p0161-private-data-model")]["metadata"]["namespace"]
        self.assertEqual(self.np["metadata"]["namespace"], ns)


class TestThreatModelDoc(unittest.TestCase):
    def test_readme_mentions_trifecta(self):
        # 受入 verify と同じ条件。README が脅威モデルに言及し続けることを固定する
        readme = (PROFILE / "README.md").read_text()
        self.assertTrue(re.search(r"trifecta|三要素", readme),
                        "README に trifecta / 三要素への言及が無い")

    def test_readme_covers_all_three_elements(self):
        readme = (PROFILE / "README.md").read_text()
        for needle in ("私的データ", "信頼できない内容", "外部への送信経路"):
            self.assertIn(needle, readme, f"脅威モデルの要素「{needle}」の記述が無い")
        # 3 要素それぞれが「どこで断たれるか」を行レベル対応させるのが dod (2)。
        # 対応表の中身まで見ないが、断つ場所となる 3 ファイル名が全て登場することは見る
        for fname in ("job.yaml", "networkpolicy.yaml", "secret-fixture.yaml"):
            self.assertIn(fname, readme)

    def test_readme_documents_the_two_pod_deviation(self):
        # NetworkPolicy は Pod 単位 / emptyDir は Pod 内限界 — この 2 つの技術的事実と
        # PVC への置換理由が文書として残っていること。消えると次の改訂者が
        # 「なぜ同一 Pod にしないのか」を推測から始めることになる
        readme = (PROFILE / "README.md").read_text()
        self.assertIn("Pod 単位", readme)
        self.assertIn("emptyDir", readme)
        self.assertIn("p0161-handoff", readme)


class TestFixtureIsSynthetic(unittest.TestCase):
    """fixture が合成データであり続けることの機械検査 (spec why: 生活データ不接触)。"""

    REAL_DOMAIN_HINTS = (
        "gmail.com", "google.com", "outlook.com", "hotmail.com",
        "yahoo.co.jp", "yahoo.com", "icloud.com", "docomo.ne.jp",
        "softbank.ne.jp", "au.com", "line.me",
    )

    def setUp(self):
        self.secret = load_docs("secret-fixture.yaml")[0]
        self.body = "\n".join(str(v) for v in self.secret["stringData"].values())

    def test_fixture_keys_look_like_mail_files(self):
        keys = set(self.secret["stringData"])
        self.assertTrue(keys, "fixture が空")
        for key in keys:
            with self.subTest(key=key):
                self.assertTrue(key.endswith(".eml"))

    def test_every_address_domain_is_invalid_tld(self):
        # RFC 2606 の .invalid TLD は解決しない。@ を含む全ドメインを抽出して
        # 1 つでも実在しうるドメインが混ざったら red
        domains = set(re.findall(r"@([A-Za-z0-9.\-]+)", self.body))
        self.assertTrue(domains)
        for domain in domains:
            with self.subTest(domain=domain):
                self.assertTrue(
                    domain.endswith(".invalid"),
                    f"{domain} は .invalid ではない — 合成データの規約を守ること",
                )

    def test_fixture_contains_no_real_provider_names(self):
        lowered = self.body.lower()
        for hint in self.REAL_DOMAIN_HINTS:
            self.assertNotIn(hint, lowered, f"実在ドメイン {hint} が混入している")

    def test_each_mail_carries_synthetic_marker(self):
        for key, value in self.secret["stringData"].items():
            with self.subTest(key=key):
                self.assertIn("[SYNTHETIC]", value)


if __name__ == "__main__":
    unittest.main()

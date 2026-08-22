"""ops/check_credential_map.py の純粋関数と実リポジトリ走査を固定する (P-0071)。

リポジトリルートから `python3 -m unittest discover -s ops/tests -t .`。
実リポジトリだけを見る検査は「今たまたま通っている」と「正しい」を区別できないので、
純関数 (find_violations) 側は合成入力で両方向を固定する。
"""

import tempfile
import unittest
from pathlib import Path

from ops import check_credential_map as ccm

# 合成 fixture の共通土台。宣言済みの鍵/Secret 名と、それらを使う ExternalSecret が
# 1 つずつある「整合している状態」
KEY_A = "MY_DOPPLER_KEY"
SECRET_A = "myapp-credentials"


def clean_refs() -> ccm.CredentialRefs:
    return ccm.CredentialRefs(
        doppler_keys={KEY_A},
        secret_targets={SECRET_A},
        created_secrets={("ns-a", SECRET_A)},
        consumed_secrets={("ns-a", SECRET_A)},
    )


def find(refs, keys=frozenset({KEY_A}), targets=frozenset({SECRET_A}), exempt=None):
    return ccm.find_violations(
        refs,
        frozenset(keys),
        frozenset(targets),
        exempt if exempt is not None else {},
    )


class TestFindViolations(unittest.TestCase):
    """判定の両方向を合成入力で固定する。"""

    def test_consistent_state_passes(self):
        self.assertEqual(find(clean_refs()), [])

    def test_undeclared_doppler_key_fails(self):
        refs = clean_refs()
        refs.doppler_keys.add("NEW_SECRET_KEY")
        violations = find(refs)
        self.assertEqual(len(violations), 1)
        self.assertIn("NEW_SECRET_KEY", violations[0])
        self.assertIn("DECLARED_DOPPLER_KEYS", violations[0])

    def test_stale_doppler_declaration_fails(self):
        """参照が消えたのに宣言が残っていたら落とす。腐った地図は地図ではない。"""
        refs = clean_refs()
        violations = find(refs, keys=frozenset({KEY_A, "GONE_KEY"}))
        self.assertEqual(len(violations), 1)
        self.assertIn("GONE_KEY", violations[0])

    def test_undeclared_secret_target_fails(self):
        refs = clean_refs()
        refs.secret_targets.add("another-app-secret")
        refs.created_secrets.add(("ns-b", "another-app-secret"))
        violations = find(refs)
        self.assertEqual(len(violations), 1)
        self.assertIn("another-app-secret", violations[0])

    def test_stale_secret_target_declaration_fails(self):
        violations = find(clean_refs(), targets=frozenset({SECRET_A, "gone-secret"}))
        self.assertEqual(len(violations), 1)
        self.assertIn("gone-secret", violations[0])

    def test_consumer_without_creator_fails(self):
        """参照だけあって作り手が無い = apply した瞬間に落ちる構成。"""
        refs = clean_refs()
        refs.consumed_secrets.add(("ns-a", "nobody-creates-this"))
        violations = find(refs)
        self.assertEqual(len(violations), 1)
        self.assertIn("nobody-creates-this", violations[0])
        self.assertIn("EXEMPT_SECRET_CONSUMERS", violations[0])

    def test_same_name_other_namespace_is_not_a_creator(self):
        """Secret は namespace スコープ。別 namespace の同名は作り手になれない。"""
        refs = clean_refs()
        refs.consumed_secrets.add(("ns-b", SECRET_A))
        violations = find(refs)
        self.assertEqual(len(violations), 1)
        self.assertIn("ns-b/" + SECRET_A, violations[0])

    def test_exempt_consumer_passes(self):
        refs = clean_refs()
        refs.consumed_secrets.add(("helm-ns", "chart-made-secret"))
        exempt = {("helm-ns", "chart-made-secret"): "immich chart が作る"}
        self.assertEqual(find(refs, exempt=exempt), [])

    def test_stale_exemption_fails(self):
        """免除された Secret が repo 内で作られるようになったら免除は不要。"""
        exempt = {("ns-a", SECRET_A): "昔は chart が作っていた"}
        violations = find(clean_refs(), exempt=exempt)
        self.assertEqual(len(violations), 1)
        self.assertIn("免除は不要", violations[0])

    def test_empty_scan_fails_closed(self):
        """走査が何も見つけられなかったら、整合していても落とす。"""
        empty = ccm.CredentialRefs()
        violations = find(empty, keys=frozenset(), targets=frozenset())
        self.assertEqual(len(violations), 1)
        self.assertIn("1 つも見つけられなかった", violations[0])


class TestScanApps(unittest.TestCase):
    """走査側の振る舞いを合成 manifest で固定する。"""

    def write_app(self, tmp: Path, name: str, text: str):
        app_dir = tmp / name
        app_dir.mkdir()
        (app_dir / "manifest.yaml").write_text(text)

    def scan_text(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.write_app(tmp, "myapp", text)
            return ccm.scan_apps(tmp)

    def test_cronjob_jobtemplate_is_walked(self):
        """CronJob の pod spec は一段深い。ここを素通りすると env が黙って抜ける
        (初期測定で実際に取りこぼした形)。"""
        refs, problems = self.scan_text(
            """
apiVersion: batch/v1
kind: CronJob
metadata:
  name: backup
  namespace: myapp
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: restic/restic
              env:
                - name: RESTIC_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: my-restic-credentials
                      key: RESTIC_PASSWORD
"""
        )
        self.assertEqual(problems, [])
        self.assertIn(("myapp", "my-restic-credentials"), refs.consumed_secrets)

    def test_envfrom_secretref_is_consumed(self):
        refs, problems = self.scan_text(
            """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: myapp
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx
          envFrom:
            - secretRef:
                name: web-env
"""
        )
        self.assertEqual(problems, [])
        self.assertIn(("myapp", "web-env"), refs.consumed_secrets)

    def test_external_secret_without_target_name_falls_back_to_metadata_name(self):
        """ESO は target.name 省略時、ExternalSecret と同名の Secret を作る。"""
        refs, problems = self.scan_text(
            """
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: fallback-name
  namespace: myapp
spec:
  data:
    - secretKey: TOKEN
      remoteRef:
        key: SOME_KEY
"""
        )
        self.assertEqual(problems, [])
        self.assertIn("fallback-name", refs.secret_targets)
        self.assertIn("SOME_KEY", refs.doppler_keys)

    def test_datafrom_is_fail_closed(self):
        """dataFrom はキーを列挙できない。成功扱いにせず落とす。"""
        _, problems = self.scan_text(
            """
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: bulk
  namespace: myapp
spec:
  dataFrom:
    - extract:
        key: WHOLE_PROJECT
"""
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("dataFrom", problems[0])

    def test_missing_remote_ref_key_is_fail_closed(self):
        _, problems = self.scan_text(
            """
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: broken
  namespace: myapp
spec:
  data:
    - secretKey: TOKEN
      remoteRef: {}
"""
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("remoteRef.key", problems[0])

    def test_broken_yaml_is_fail_closed(self):
        _, problems = self.scan_text("kind: [ExternalSecret\n  bad:: :yaml")
        self.assertEqual(len(problems), 1)
        self.assertIn("YAML が読めない", problems[0])


class TestRealRepo(unittest.TestCase):
    """今のリポジトリが「実態 = 宣言」であることの確認。

    ここで違反が出たら、apps/ を変えた人が check_credential_map.py の地図も
    一緒に変えていない。地図を更新するのが正 (参照追加時) か、宣言を消すのが
    正 (廃止時) かはエラーメッセージが案内する。
    """

    def test_real_repo_matches_declarations(self):
        refs, problems = ccm.scan_apps(ccm.APPS_DIR)
        self.assertEqual(
            problems, [], "\n".join(problems)
        )
        violations = ccm.find_violations(
            refs, ccm.DECLARED_DOPPLER_KEYS, ccm.DECLARED_SECRET_TARGETS,
            ccm.EXEMPT_SECRET_CONSUMERS,
        )
        self.assertEqual(violations, [], "\n" + "\n".join(violations))

    def test_scan_actually_sees_something(self):
        """走査そのものが壊れて空を返すと、上のテストは黙って通ってしまう。

        ランドマークを直接見る。特に restic CronJob 由来の参照は、CronJob の
        jobTemplate を辿れていることの証明になる (辿れないと黙って消える)。
        """
        refs, _ = ccm.scan_apps(ccm.APPS_DIR)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", refs.doppler_keys)
        self.assertIn("autopilot-credentials", refs.secret_targets)
        self.assertIn(("syncthing", "syncthing-restic-backup-credentials"), refs.consumed_secrets)
        self.assertIn(("vaultwarden", "vaultwarden-admin-token"), refs.consumed_secrets)


if __name__ == "__main__":
    unittest.main()

"""P-0080 restore drill の判定ロジックを、cluster 接続なしで固定するテスト。

CI の ops job は ubuntu-latest + python3 だけで回る (.github/workflows/ci.yml)。そのため
ops/drills/restore_drill.py は「計画生成・manifest 生成・RTO 計算・report 検査」を純関数に
分離してあり、このテストは合成入力で両方向 (落ちること / 通ること) を固定する。

liveness 判定は「スクリプト内の定数」が唯一の実装なので (PROJECT.md 設計方針 2)、
**テストからもその定数そのものを実行する**。VAULTWARDEN_CHECK_PY /
SYNCTHING_CHECK_PY は python プロセスとして、IMMICH_LIVENESS_SH /
WORKSPACE_HOME_LIVENESS_SH は sh プロセスとして、DRILL_BASE を合成した復元結果の木に
向けて走らせる。本物の restic/postgres は要らないよう、restic だけは PATH 上の stub で
`ls` サブコマンドの出力を固定する。pg-dump 関係 (PG_CHECK_SH) は psql/pg_restore が
ローカルに無いため実行せず、内容のアサーションのみ行う。

リポジトリルートから `python3 -m unittest ops.tests.test_restore_drill`。
"""

import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ops.drills.restore_drill import (
    IMMICH_LIVENESS_SH,
    LIVENESS_CRITERIA,
    POSTGRES_IMAGE,
    RESTIC_IMAGE,
    TARGETS,
    VAULTWARDEN_CHECK_PY,
    SYNCTHING_CHECK_PY,
    WORKSPACE_HOME_LIVENESS_SH,
    build_externalsecret_manifest,
    build_job_manifest,
    build_pvc_manifest,
    compute_rto_seconds,
    expand_units,
    in_forbidden_jst_window,
    parse_k8s_timestamp,
    restore_script,
    validate_report,
)

WORKSPACES = [
    "0cd09458-ec4b-4b17-9039-f4e4f5927305",
    "7fdb7787-e2b7-4a6d-b54f-1640b5d9b587",
]
UNITS = expand_units(TARGETS, WORKSPACES)
JOBS = {u["job_name"]: build_job_manifest(u) for u in UNITS}

# 本番 PVC 名。drill Job の volume がこれらを参照していないことを必ず検査する
# (「本番 PVC には触らない」の機械的保証)
PROD_PVC_NAMES = {
    "vaultwarden-data", "immich-library", "coder-postgres-data", "syncthing-data",
}


def _pod_spec(job: dict) -> dict:
    return job["spec"]["template"]["spec"]


def _all_containers(job: dict) -> list[dict]:
    pod = _pod_spec(job)
    return pod.get("initContainers", []) + pod.get("containers", [])


def _run_py(script: str, base: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "DRILL_BASE": str(base)},
        capture_output=True, text=True,
    )


def _run_sh(script: str, base: Path, fake_restic_lines: list[str] | None = None) \
        -> subprocess.CompletedProcess:
    env = {**os.environ, "DRILL_BASE": str(base),
           "WORKSPACE_ID": "0cd09458-ec4b-4b17-9039-f4e4f5927305"}
    if fake_restic_lines is not None:
        bin_dir = base.parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "restic"
        # ls サブコマンドに対して固定出力を返すだけの stub。実 Pod では本物の restic が
        # 同一コンテナ内で動く (restore → ls → find の突き合わせ)
        lines = "\n".join(fake_restic_lines)
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = ls ]; then\n'
            "printf '%s\\n' '" + lines + "'\n"
            "fi\n"
        )
        stub.chmod(0o755)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)


class TestTargets(unittest.TestCase):
    def test_five_targets_with_expected_names(self):
        self.assertEqual(
            [t["name"] for t in TARGETS],
            ["vaultwarden-data", "immich-library", "coder-postgres-data",
             "coder-workspace-homes", "syncthing-data"],
        )

    def test_liveness_criteria_covers_every_kind(self):
        kinds = {t["kind"] for t in TARGETS}
        self.assertEqual(set(LIVENESS_CRITERIA), kinds)

    def test_all_credentials_are_append_only(self):
        for target in TARGETS:
            self.assertTrue(
                target["secret_name"].endswith("-restic-backup-credentials"),
                f"{target['name']}: 削除鍵 (<app>-restic-credentials) を参照してはいけない",
            )

    def test_namespaces_and_repos_match_production_wiring(self):
        # (target 名, 本番 namespace): (restic リポジトリ末尾, drill namespace の末尾)。
        # apps/*/restic-backup-cronjob.yaml の RESTIC_REPOSITORY と 1 対 1 で対応する
        expected = {
            ("vaultwarden-data", "vaultwarden"): ("vaultwarden", "vaultwarden"),
            ("immich-library", "immich"): ("immich", "immich"),
            ("coder-postgres-data", "coder"): ("coder-postgres", "coder-postgres"),
            ("syncthing-data", "syncthing"): ("syncthing", "syncthing"),
        }
        for target in TARGETS:
            key = (target["name"], target["source_namespace"])
            if key in expected:
                repo_suffix, drill_tail = expected[key]
                self.assertEqual(target["repo_suffix"], repo_suffix)
                self.assertEqual(target["drill_namespace"], f"drill-restore-{drill_tail}")


class TestForbiddenWindow(unittest.TestCase):
    @staticmethod
    def utc(y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)

    def test_outside_window_is_allowed(self):
        self.assertFalse(in_forbidden_jst_window(self.utc(2026, 8, 22, 13, 13)))  # JST 22:13

    def test_backup_band_boundaries(self):
        # JST 02:39 = UTC 前日 17:39 → 許可
        self.assertFalse(in_forbidden_jst_window(self.utc(2026, 8, 21, 17, 39)))
        # JST 02:40 (immich backup の 5 分前) から禁止
        self.assertTrue(in_forbidden_jst_window(self.utc(2026, 8, 21, 17, 40)))
        # JST 04:59 も禁止 (retention の帯が終わる直前)
        self.assertTrue(in_forbidden_jst_window(self.utc(2026, 8, 21, 19, 59)))
        # JST 05:00 で解禁
        self.assertFalse(in_forbidden_jst_window(self.utc(2026, 8, 21, 20, 0)))


class TestRto(unittest.TestCase):
    def test_parse_k8s_timestamp_keeps_utc(self):
        ts = parse_k8s_timestamp("2026-08-22T13:00:00Z")
        self.assertEqual(ts.tzinfo.utcoffset(ts), timedelta(0))
        self.assertEqual(ts.hour, 13)

    def test_basic_rto(self):
        rto = compute_rto_seconds("2026-08-22T13:00:00Z", "2026-08-22T13:01:30Z")
        self.assertEqual(rto, 90)

    def test_same_moment_is_zero(self):
        self.assertEqual(compute_rto_seconds("2026-08-22T13:00:00Z", "2026-08-22T13:00:00Z"), 0)

    def test_sub_second_is_floored_not_rounded(self):
        """誇張しない (切り上げない) ための方向付け。0.9 秒は 0 扱い。"""
        rto = compute_rto_seconds("2026-08-22T13:00:00Z", "2026-08-22T13:00:00.900000Z")
        self.assertEqual(rto, 0)


def _valid_report() -> dict:
    return {
        "targets": [
            {"name": name, "namespace": f"drill-restore-{i}", "rto_seconds": i * 10}
            for i, name in enumerate(sorted({t["name"] for t in TARGETS}))
        ]
    }


class TestValidateReport(unittest.TestCase):
    def test_valid_report_passes(self):
        self.assertEqual(validate_report(_valid_report()), [])

    def test_fewer_than_five_targets_fails(self):
        report = _valid_report()
        report["targets"] = report["targets"][:4]
        problems = validate_report(report)
        self.assertEqual(len(problems), 1)
        self.assertIn("5 未満", problems[0])

    def test_null_rto_fails(self):
        """失敗対象を null で誤魔化した report は合格させない (PROJECT.md 方針 6)。"""
        report = _valid_report()
        report["targets"][0]["rto_seconds"] = None
        problems = validate_report(report)
        self.assertTrue(any("rto_seconds" in p for p in problems))

    def test_missing_rto_key_fails(self):
        report = _valid_report()
        del report["targets"][2]["rto_seconds"]
        problems = validate_report(report)
        self.assertTrue(any("rto_seconds" in p for p in problems))

    def test_boolean_rto_fails(self):
        report = _valid_report()
        report["targets"][1]["rto_seconds"] = True  # isinstance(True, int) なので明示排除
        problems = validate_report(report)
        self.assertTrue(any("rto_seconds" in p for p in problems))

    def test_duplicate_target_name_fails(self):
        report = _valid_report()
        report["targets"][1]["name"] = report["targets"][0]["name"]
        problems = validate_report(report)
        self.assertTrue(any("重複" in p for p in problems))

    def test_non_drill_namespace_fails(self):
        report = _valid_report()
        report["targets"][0]["namespace"] = "vaultwarden"  # 本番 namespace での復元は仕様違反
        problems = validate_report(report)
        self.assertTrue(any("drill-" in p for p in problems))


class TestExpandUnits(unittest.TestCase):
    def test_workspace_homes_expand_per_workspace(self):
        self.assertEqual(len(UNITS), 6)  # 4 対象 + workspace-home × 2
        ws_units = [u for u in UNITS if u["kind"] == "workspace-home"]
        self.assertEqual(len(ws_units), 2)
        self.assertEqual([u["workspace_id"] for u in ws_units], WORKSPACES)

    def test_non_workspace_units_have_no_workspace_id(self):
        others = [u for u in UNITS if u["kind"] != "workspace-home"]
        self.assertEqual(len(others), 4)
        self.assertTrue(all(u["workspace_id"] is None for u in others))

    def test_unit_names_are_unique(self):
        job_names = [u["job_name"] for u in UNITS]
        pvc_names = [u["pvc_name"] for u in UNITS]
        self.assertEqual(len(set(job_names)), len(job_names))
        self.assertEqual(len(set(pvc_names)), len(pvc_names))

    def test_all_units_live_in_drill_namespaces(self):
        for unit in UNITS:
            self.assertTrue(unit["namespace"].startswith("drill-"), unit["namespace"])

    def test_empty_workspaces_yield_four_units(self):
        units = expand_units(TARGETS, [])
        self.assertEqual(len(units), 4)


class TestManifestSafety(unittest.TestCase):
    def test_job_basics(self):
        for unit in UNITS:
            job = JOBS[unit["job_name"]]
            spec = job["spec"]
            self.assertEqual(spec["backoffLimit"], 0,
                             "失敗をリトライで隠さない (backoffLimit 0)")
            self.assertGreater(spec["activeDeadlineSeconds"], 0)
            pod = spec["template"]["spec"]
            self.assertIs(pod.get("automountServiceAccountToken"), False)
            self.assertEqual(pod.get("restartPolicy"), "Never")

    def test_volumes_reference_only_the_drill_pvc(self):
        """本番 PVC への参照は 1 つもあってはならない (DoD「本番 PVC には触らない」)。"""
        for unit in UNITS:
            job = JOBS[unit["job_name"]]
            claims = []
            for vol in _pod_spec(job).get("volumes", []):
                claim = vol.get("persistentVolumeClaim", {}).get("claimName")
                if claim:
                    claims.append(claim)
            self.assertEqual(claims, [unit["pvc_name"]],
                             f"{unit['job_name']}: 参照する PVC は drill 側のみであるべき")
            for claim in claims:
                self.assertNotIn(claim, PROD_PVC_NAMES)
                self.assertTrue(claim.startswith("drill-pvc-"))

    def test_restore_containers_have_required_capabilities(self):
        """CHOWN/FOWNER/DAC_OVERRIDE + restore 前 rm -rf は docs/backup.md の教訓。"""
        for unit in UNITS:
            job = JOBS[unit["job_name"]]
            for container in _all_containers(job):
                if container.get("image") != RESTIC_IMAGE:
                    continue
                caps = container["securityContext"]["capabilities"]
                self.assertEqual(set(caps["drop"]), {"ALL"})
                for required in ("CHOWN", "FOWNER", "DAC_OVERRIDE"):
                    self.assertIn(required, caps["add"],
                                  f"{unit['job_name']}/{container['name']} に {required} が無い")

    def test_restore_scripts_cleanup_before_restore(self):
        script = restore_script("/data")
        self.assertLess(script.index("rm -rf"), script.index("restic restore latest"))
        with_host = restore_script("/data", '--host "$WORKSPACE_ID" ')
        self.assertIn('restic restore latest --host "$WORKSPACE_ID" --target /data', with_host)

    def test_env_references_append_only_secret_and_correct_repo(self):
        for unit in UNITS:
            job = JOBS[unit["job_name"]]
            restic_containers = [c for c in _all_containers(job)
                                 if c.get("image") == RESTIC_IMAGE]
            self.assertTrue(restic_containers, unit["job_name"])
            for container in restic_containers:
                env = {e["name"]: e for e in container["env"]}
                repo = env["RESTIC_REPOSITORY"]["value"]
                self.assertEqual(repo, f"b2:$(RESTIC_B2_BUCKET):{unit['repo_suffix']}")
                # k8s の $(VAR) 展開は前方定義のみ参照できる。順序が崩れると bucket 名が
                # 空になり "parsing repository location failed" で落ちる (2026-08-22 実測)
                names = [e["name"] for e in container["env"]]
                self.assertLess(names.index("RESTIC_B2_BUCKET"),
                                names.index("RESTIC_REPOSITORY"))
                for key in ("RESTIC_PASSWORD", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY"):
                    ref = env[key]["valueFrom"]["secretKeyRef"]
                    self.assertEqual(ref["name"], unit["secret_name"])
                    self.assertTrue(ref["name"].endswith("-restic-backup-credentials"))

    def test_sqlite_and_syncthing_use_python_check_after_initcontainer(self):
        for kind in ("sqlite", "syncthing-config"):
            unit = next(u for u in UNITS if u["kind"] == kind)
            job = JOBS[unit["job_name"]]
            pod = _pod_spec(job)
            self.assertEqual(len(pod["initContainers"]), 1)
            self.assertEqual(pod["initContainers"][0]["image"], RESTIC_IMAGE)
            self.assertEqual(pod["containers"][0]["image"], "python:3.12-alpine")
            self.assertIn("-c", pod["containers"][0]["command"])

    def test_immich_single_container_does_both(self):
        unit = next(u for u in UNITS if u["kind"] == "library-with-db-dump")
        container = _all_containers(JOBS[unit["job_name"]])[0]
        self.assertEqual(container["image"], RESTIC_IMAGE)
        self.assertIn("restic restore latest", container["command"][2])
        self.assertIn("gzip -cd", container["command"][2])

    def test_pg_dump_pod_layout(self):
        unit = next(u for u in UNITS if u["kind"] == "pg-dump")
        pod = _pod_spec(JOBS[unit["job_name"]])
        images = {c["name"]: c["image"] for c in pod["containers"]}
        self.assertEqual(images, {"postgres-server": POSTGRES_IMAGE,
                                  "restore-check": POSTGRES_IMAGE})
        init_mounts = pod["initContainers"][0]["volumeMounts"]
        self.assertEqual(init_mounts[0]["name"], "dump")
        server_mounts = {m["name"] for m in pod["containers"][0]["volumeMounts"]}
        check_mounts = {m["name"] for m in pod["containers"][1]["volumeMounts"]}
        self.assertIn("coord", server_mounts & check_mounts,
                      "完了マーカー (/coord/done) は server と check の両方が見える必要がある")
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertIn("emptyDir", volumes["dump"])
        self.assertIn("persistentVolumeClaim", volumes["pgdata"])
        self.assertEqual(volumes["pgdata"]["persistentVolumeClaim"]["claimName"],
                         unit["pvc_name"])

    def test_workspace_home_carries_workspace_id(self):
        for unit in (u for u in UNITS if u["kind"] == "workspace-home"):
            container = _all_containers(JOBS[unit["job_name"]])[0]
            env = {e["name"]: e.get("value") for e in container["env"]}
            self.assertEqual(env["WORKSPACE_ID"], unit["workspace_id"])

    def test_externalsecret_pulls_append_only_keys_only(self):
        es = build_externalsecret_manifest("drill-x", "vaultwarden-restic-backup-credentials")
        self.assertEqual(es["spec"]["secretStoreRef"],
                         {"kind": "ClusterSecretStore", "name": "doppler"})
        remote_keys = [d["remoteRef"]["key"] for d in es["spec"]["data"]]
        self.assertIn("B2_ACCOUNT_ID_APPEND_ONLY", remote_keys)
        self.assertIn("B2_ACCOUNT_KEY_APPEND_ONLY", remote_keys)
        # 削除権限つき鍵 (plain) を持ち出さない — 復元は readFiles で足りる (P-0028 実測)
        self.assertNotIn("B2_ACCOUNT_ID", remote_keys)
        self.assertNotIn("B2_ACCOUNT_KEY", remote_keys)
        secret_keys = {d["secretKey"] for d in es["spec"]["data"]}
        self.assertEqual(secret_keys,
                         {"RESTIC_PASSWORD", "RESTIC_B2_BUCKET",
                          "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY"})

    def test_pvc_uses_local_path(self):
        pvc = build_pvc_manifest("drill-x", "drill-pvc-x", "1Gi")
        self.assertEqual(pvc["spec"]["storageClassName"], "local-path")
        self.assertEqual(pvc["spec"]["accessModes"], ["ReadWriteOnce"])


class TestLivenessScriptsExecuted(unittest.TestCase):
    """判定定数そのものを合成データで走らせ、OK と NG の両方向を固定する。"""

    def _make_vaultwarden_fixture(self, base: Path) -> Path:
        (base / "mnt/vaultwarden-data").mkdir(parents=True)
        (base / "mnt/vaultwarden-data/rsa_key.pem").write_text("-----BEGIN KEY-----")
        staging = base / "staging"
        staging.mkdir()
        con = sqlite3.connect(staging / "db.sqlite3")
        con.execute("CREATE TABLE t1 (id integer)")
        con.commit()
        con.close()
        return staging / "db.sqlite3"

    def test_vaultwarden_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = self._make_vaultwarden_fixture(base)
            proc = _run_py(VAULTWARDEN_CHECK_PY, base)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("VAULTWARDEN_LIVENESS_OK", proc.stdout)
            self.assertIn(str(db), proc.stdout)
            self.assertIn("rsa_keys=1", proc.stdout)

    def test_vaultwarden_corrupt_db_fails_integrity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db = self._make_vaultwarden_fixture(base)
            # マジックバイトは正しいが中身が壊れた SQLite
            raw = bytearray(db.read_bytes())
            raw[64:128] = b"\xde\xad" * 32
            db.write_bytes(bytes(raw))
            proc = _run_py(VAULTWARDEN_CHECK_PY, base)
            self.assertNotEqual(proc.returncode, 0)

    def test_vaultwarden_wrong_magic_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_vaultwarden_fixture(base)
            (base / "staging/db.sqlite3").write_bytes(b"NOT A DATABASE" * 10)
            proc = _run_py(VAULTWARDEN_CHECK_PY, base)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("magic", proc.stderr)

    def test_vaultwarden_missing_db_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_py(VAULTWARDEN_CHECK_PY, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)

    def _make_syncthing_fixture(self, base: Path) -> Path:
        cfg = base / "mnt/syncthing-data/config"
        cfg.mkdir(parents=True)
        (cfg / "config.xml").write_text(
            "<?xml version='1.0'?>\n<config><gui enabled='true'>"
            "<address>127.0.0.1:8384</address></gui></config>\n"
        )
        (cfg / "cert.pem").write_text("cert\n")
        (cfg / "key.pem").write_text("key\n")
        return cfg

    def test_syncthing_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_syncthing_fixture(Path(tmp))
            proc = _run_py(SYNCTHING_CHECK_PY, Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("SYNCTHING_LIVENESS_OK", proc.stdout)
            self.assertIn("<config>", proc.stdout)
            self.assertIn(str(cfg), proc.stdout)

    def test_syncthing_broken_xml_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_syncthing_fixture(Path(tmp))
            (cfg / "config.xml").write_text("<config><unclosed>")
            proc = _run_py(SYNCTHING_CHECK_PY, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)

    def test_syncthing_missing_identity_fails(self):
        """cert/key.pem はデバイス ID 本体。片方でも欠けたら生きていない。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._make_syncthing_fixture(Path(tmp))
            (cfg / "key.pem").unlink()
            proc = _run_py(SYNCTHING_CHECK_PY, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("missing", proc.stderr)

    def _make_immich_fixture(self, base: Path, dump_bytes: bytes | None) -> None:
        backups = base / "mnt/immich-library/backups"
        backups.mkdir(parents=True)
        upload = base / "mnt/immich-library/upload"
        upload.mkdir(parents=True)
        (upload / "photo.jpg").write_bytes(b"\xff\xd8\xff")
        if dump_bytes is not None:
            (backups / "immich-db-backup-20260822T000000-v2.7.5-pg16.9.sql.gz") \
                .write_bytes(dump_bytes)

    def test_immich_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_immich_fixture(Path(tmp), gzip.compress(b"-- pg dump body"))
            proc = _run_sh(IMMICH_LIVENESS_SH, Path(tmp))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("IMMICH_LIVENESS_OK", proc.stdout)
            self.assertIn("library_files=2", proc.stdout)

    def test_immich_without_dump_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make_immich_fixture(Path(tmp), None)
            proc = _run_sh(IMMICH_LIVENESS_SH, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("IMMICH_LIVENESS_FAIL", proc.stdout)

    def test_immich_truncated_gzip_fails(self):
        """gzip -cd の失敗を見逃さない (pipefail)。途中まで展開できても生きていない扱い。"""
        with tempfile.TemporaryDirectory() as tmp:
            self._make_immich_fixture(Path(tmp), b"\x1f\x8b\x08broken-truncated-garbage")
            proc = _run_sh(IMMICH_LIVENESS_SH, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)

    # restic ls 0.19.1 の平文出力は dir に末尾 / を付けない (2026-08-22 実測。
    # T-0071 の「3904 files/dirs vs find 3156」と同じ事実)。dir もそのまま並ぶ
    LS_LINES_OK = [
        "snapshot abc12345 of [/staging] at 2026-08-22 filtered by []",
        "/staging/.bashrc",
        "/staging/projects",
        "/staging/projects/app",
        "/staging/projects/app/main.py",
    ]

    def test_workspace_home_ok_when_counts_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            (base / "projects/app").mkdir(parents=True)
            (base / ".bashrc").write_text("export EDITOR=vim\n")
            (base / "projects/app/main.py").write_text("print(1)\n")
            proc = _run_sh(WORKSPACE_HOME_LIVENESS_SH, base,
                           fake_restic_lines=self.LS_LINES_OK)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("WORKSPACE_HOME_LIVENESS_OK", proc.stdout)
            self.assertIn("entries=4", proc.stdout)

    def test_workspace_home_count_mismatch_fails(self):
        """restic ls と復元結果の突き合わせ (PROJECT.md 方針 2)。欠けたら失敗。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            (base / "projects").mkdir(parents=True)
            (base / ".bashrc").write_text("export EDITOR=vim\n")
            proc = _run_sh(WORKSPACE_HOME_LIVENESS_SH, base,
                           fake_restic_lines=self.LS_LINES_OK)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("WORKSPACE_HOME_LIVENESS_FAIL", proc.stdout)

    def test_workspace_home_empty_restore_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir(parents=True)
            proc = _run_sh(WORKSPACE_HOME_LIVENESS_SH, base,
                           fake_restic_lines=self.LS_LINES_OK)
            self.assertNotEqual(proc.returncode, 0)


class TestPgCheckScriptContent(unittest.TestCase):
    """psql/pg_restore はローカルに無いため実行ではなく内容で固定する。
    (実機での成否自体は drill 実行時に Job の成否として現れる)"""

    def test_pg_check_requires_readiness_restore_and_tables(self):
        from ops.drills.restore_drill import PG_CHECK_SH
        self.assertIn("pg_isready -h 127.0.0.1", PG_CHECK_SH)
        self.assertIn("--exit-on-error", PG_CHECK_SH)
        self.assertIn("-d drill", PG_CHECK_SH)
        self.assertIn("public_tables=$TABLES", PG_CHECK_SH)
        self.assertIn("[ \"$TABLES\" -gt 0 ]", PG_CHECK_SH)
        self.assertIn("touch /coord/done", PG_CHECK_SH)


# 2026-08-22 の実走で B2 が返した 403 の実文。両形式とも cap 超過と判定できなければならない
REAL_RESTIC_CAP_LOG = (
    "Stat(<config/>) returned error, retrying after 1m7.830475013s: "
    "Stat: b2_download_file_by_name: 403: Cannot download file, download bandwidth or "
    "transaction (Class B) cap exceeded. See the Caps & Alerts page to increase your cap."
)
REAL_RAW_API_BODY = json.dumps({
    "code": "download_cap_exceeded",
    "message": "Cannot download file, download bandwidth or transaction (Class B) cap "
               "exceeded. See the Caps & Alerts page to increase your cap.",
    "status": 403,
})


class TestDownloadCapDetection(unittest.TestCase):
    """B2 download cap 超過の検出。これを見逃すと全 unit が 25 分のリトライに溶ける
    (2026-08-22 の失敗 run がまさにそれ)。"""

    def test_detects_real_restic_log_line(self):
        from ops.drills.restore_drill import is_download_cap_error
        self.assertTrue(is_download_cap_error(REAL_RESTIC_CAP_LOG))

    def test_detects_raw_api_body(self):
        from ops.drills.restore_drill import is_download_cap_error
        self.assertTrue(is_download_cap_error(REAL_RAW_API_BODY))

    def test_ignores_other_403(self):
        from ops.drills.restore_drill import is_download_cap_error
        other = "Stat: b2_download_file_by_name: 403: unauthorized"
        self.assertFalse(is_download_cap_error(other))

    def test_ignores_empty_and_unrelated_logs(self):
        from ops.drills.restore_drill import is_download_cap_error
        self.assertFalse(is_download_cap_error(""))
        self.assertFalse(is_download_cap_error("restic 0.19.1 restored 9 files"))


class TestPreflightProbeJob(unittest.TestCase):
    """phase 0 の preflight probe Job。最小リポジトリ 1 本で download 可否を先に見る。"""

    SYNCTHING = [t for t in TARGETS if t["name"] == "syncthing-data"][0]

    def test_probe_job_shape(self):
        from ops.drills.restore_drill import (
            PREFLIGHT_NAMESPACE,
            PROBE_ACTIVE_DEADLINE_SECONDS,
            PROBE_JOB_NAME,
            build_probe_job,
        )
        job = build_probe_job(self.SYNCTHING["secret_name"], self.SYNCTHING["repo_suffix"])
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["metadata"]["namespace"], PREFLIGHT_NAMESPACE)
        self.assertEqual(job["metadata"]["namespace"].startswith("drill-"), True)
        self.assertEqual(job["metadata"]["name"], PROBE_JOB_NAME)
        spec = job["spec"]
        self.assertEqual(spec["backoffLimit"], 0)
        self.assertEqual(spec["activeDeadlineSeconds"], PROBE_ACTIVE_DEADLINE_SECONDS)
        pod = spec["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["image"], RESTIC_IMAGE)
        # snapshots の読み取りは config/index の download を伴う = download 可否の試金石
        self.assertEqual(container["command"][0], "restic")
        self.assertEqual(container["command"][1], "snapshots")
        env_names = {e["name"] for e in container["env"]}
        self.assertEqual(
            env_names,
            {"RESTIC_B2_BUCKET", "RESTIC_REPOSITORY",
             "RESTIC_PASSWORD", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY"},
        )

    def test_preflight_target_is_the_smallest_repo(self):
        """probe の対象は syncthing 固定 (run_drill が TARGETS[-1] を渡す)。
        意図せず workspace-home 等の大リポジトリを引くと Class B 予算を溶かす。"""
        from ops.drills.restore_drill import TARGETS
        self.assertEqual(TARGETS[-1]["repo_suffix"], "syncthing")
        self.assertEqual(TARGETS[-1]["pvc_size"], "1Gi")


if __name__ == "__main__":
    unittest.main()

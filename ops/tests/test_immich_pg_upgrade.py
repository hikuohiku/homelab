"""apps/immich/postgres.yaml の cloudnative-vectorchord 16.14 化対策を形で固定する (P-0092)。

なぜ要るか: 16.9-0.4.3 → 16.14-1.1.1 は過去 2 回とも CrashLoopBackOff で revert している
(#244 / #257)。原因はバージョンではなく entrypoint 迂回による 2 段 FATAL で、P-0035 が
本番ダンプから作った複製上で実ログ付きで確定した (docs/immich-postgres-upgrade.md)。
対策はマニフェストの「形」としてしか存在しないので、将来の編集で差分の 1 つでも消えたら
CI が落とす。ここが無いと「起動確認だけでは見つからない」種類の事故 (docs 実測) を再演する。

固定する差分 5 点 (docs「apps/immich/postgres.yaml に必要な差分」):

  1. image を 16.14 系に — **main と init-bootstrap の両方**。bootstrap だけ旧いと
     災害復旧時に vchord 0.4.3 カタログの PGDATA が作られてしまう
  2. command: ["postgres"] の明示 (#244: args の先頭 -c がコマンド名に解釈された)
  3. init-permissions の chmod 0700 — fsGroup 999 が Pod 再作成のたび PGDATA を
     2770 に戻すので毎回要る (原因 A)。**chown と chmod を && で 1 本に繋ぐと**
     PVC 新規作成直後 (= 災害復旧そのもの) に exit 1 し、後続の init-bootstrap が
     一度も走らず Init:CrashLoopBackOff から自然回復しない (Job p-0035-rehearsal-bootstrap
     の空 PVC 実測)
  4. /var/run/postgresql へ emptyDir (postgres-run) を被せる — イメージ内の当該
     ディレクトリは owner 100:102 で uid 26 から書けない (原因 B)。
     unix_socket_directories=/tmp は livenessProbe/readinessProbe の pg_isready が
     既定パスを見るため不採用
  5. init-bootstrap (PGDATA が空のときだけ initdb → createdb → CREATE EXTENSION vchord)
     を残す — 旧イメージの ENTRYPOINT が暗黙に担っていた災害復旧経路

判定は純関数 (init_permissions_problems / init_bootstrap_problems / evaluate) に分け、
合成入力で両方向 (落ちること / 通ること) を固定する。実リポジトリだけを見るテストは
「今たまたま通っている」と「正しい」を区別できないため (test_backup_coverage.py 流儀)。
埋め込みスクリプトは sh -n / bash -n で構文だけ検査する (実行はしない)。

既知の限界 (静的検査): init-permissions の chmod がガードの「内側」にあるかは
ガード対象パスと chmod 対象パスの一致で近似する。if 文の外に置いても同名なら
見逃す。実行時の振る舞いは P-0035 の Job マニフェストが空 PVC で実測済み。

リポジトリルートから `python3 -m unittest ops.tests.test_immich_pg_upgrade`。
"""

import copy
import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "apps" / "immich" / "postgres.yaml"

IMAGE_RE = re.compile(r"^ghcr\.io/tensorchord/cloudnative-vectorchord:16\.14-\S+$")
EXPECTED_COMMAND = ["postgres"]
EXPECTED_ARGS = ["-c", "shared_preload_libraries=vchord.so"]
RUN_VOLUME = "postgres-run"
SOCKET_DIR = "/var/run/postgresql"
PGDATA_DIR = "/var/lib/postgresql/data/pgdata"
SECRET_NAME = "immich-postgres-credentials"

# 禁止形: chown と chmod を 1 行で && 連結した形 (#244 時代の形)。空 PVC で
# init-permissions 全体が exit 1 し init-bootstrap が一度も走らない。
CHAINED_CHOWN_CHMOD_RE = re.compile(r"chown\b[^\n]*&&[^\n]*chmod")
GUARD_RE = re.compile(r"if\s+\[\s+-d\s+(\S+)\s+\];\s*then")
CHMOD_RE = re.compile(r"chmod\s+0700\s+(\S+)")


# ---------------------------------------------------------------------------
# 合成 fixture (純関数の両方向テスト用)。実マニフェストと同じ形の代表値。
# ---------------------------------------------------------------------------

GOOD_IP_SCRIPT = """\
set -eu
chown -R 26:999 /var/lib/postgresql/data
if [ -d /var/lib/postgresql/data/pgdata ]; then
  chmod 0700 /var/lib/postgresql/data/pgdata
else
  echo "PGDATA does not exist yet; leaving chmod to the bootstrap path"
fi
"""

GOOD_BOOT_SCRIPT = """\
set -u
if [ -s "$PGDATA/PG_VERSION" ]; then
  echo "PGDATA already initialized, skip bootstrap"
  exit 0
fi

LOGFILE=/tmp/pg-bootstrap.log
: > "$LOGFILE"

initdb --username="$POSTGRES_USER" --pwfile=<(printf '%s\\n' "$POSTGRES_PASSWORD") $POSTGRES_INITDB_ARGS -D "$PGDATA"

echo "host all all all scram-sha-256" >> "$PGDATA/pg_hba.conf"

CREATEDB_RC=0
if [ "$POSTGRES_DB" != "postgres" ]; then
  createdb --username="$POSTGRES_USER" --host=/tmp "$POSTGRES_DB"
  CREATEDB_RC=$?
fi
if [ "$CREATEDB_RC" -ne 0 ]; then
  echo "bootstrap failed"; cat "$LOGFILE"
  exit 1
fi

psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS vchord CASCADE;"
"""


def make_good_spec():
    """docs の差分 5 点を全部満たす pod template spec の合成品。"""
    return {
        "securityContext": {"fsGroup": 999},
        "initContainers": [
            {
                "name": "init-permissions",
                "command": ["sh", "-c", GOOD_IP_SCRIPT],
                "securityContext": {"runAsUser": 0},
            },
            {
                "name": "init-bootstrap",
                "image": "ghcr.io/tensorchord/cloudnative-vectorchord:16.14-1.1.1",
                "command": ["bash", "-c", GOOD_BOOT_SCRIPT],
                "env": [
                    {"name": "POSTGRES_PASSWORD",
                     "valueFrom": {"secretKeyRef": {"name": SECRET_NAME,
                                                    "key": "password"}}},
                    {"name": "PGDATA", "value": PGDATA_DIR},
                ],
            },
        ],
        "containers": [
            {
                "name": "postgres",
                "image": "ghcr.io/tensorchord/cloudnative-vectorchord:16.14-1.1.1",
                "command": ["postgres"],
                "args": list(EXPECTED_ARGS),
                "volumeMounts": [
                    {"name": "data", "mountPath": "/var/lib/postgresql/data"},
                    {"name": RUN_VOLUME, "mountPath": SOCKET_DIR},
                ],
            }
        ],
        "volumes": [{"name": RUN_VOLUME, "emptyDir": {}}],
    }


def mutate(fn):
    spec = copy.deepcopy(make_good_spec())
    fn(spec)
    return spec


# ---------------------------------------------------------------------------
# 純関数
# ---------------------------------------------------------------------------


def load_docs(path=MANIFEST):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if isinstance(d, dict)]


def deployment_of(docs):
    return next((d for d in docs if d.get("kind") == "Deployment"), None)


def container_of(spec, name):
    for c in (spec.get("initContainers") or []) + (spec.get("containers") or []):
        if c.get("name") == name:
            return c
    return None


def script_of(container, shell):
    """sh -c / bash -c 形の command からスクリプト本文を取り出す (違形は None)。"""
    cmd = (container or {}).get("command") or []
    if len(cmd) >= 3 and cmd[0] == shell and cmd[1] == "-c":
        return cmd[2]
    return None


def init_permissions_problems(script):
    """init-permissions のスクリプト本文を受け取り問題リストを返す (空なら合格)。"""
    problems = []
    if "chown -R 26:999" not in script:
        problems.append("chown -R 26:999 が無い")
    chmods = CHMOD_RE.findall(script)
    guards = GUARD_RE.findall(script)
    if not chmods:
        problems.append(
            f"chmod 0700 {PGDATA_DIR} が無い — fsGroup が Pod 再作成のたび "
            "PGDATA を 2770 に戻すため毎回必要 (docs 原因 A)")
    elif not guards:
        problems.append(
            f"chmod が PGDATA 存在ガード (if [ -d {PGDATA_DIR} ]) で守られていない — "
            "素の chmod も && 連結も空 PVC (災害復旧) で exit 1 し自然回復しない")
    else:
        unguarded = [t for t in chmods if t not in set(guards)]
        if unguarded:
            problems.append(f"chmod 0700 {' '.join(unguarded)} が存在ガードの外にある")
    if CHAINED_CHOWN_CHMOD_RE.search(script):
        problems.append(
            "chown と chmod を && で 1 行に連結している (禁止形: 空 PVC で "
            "init-bootstrap が走らず災害復旧が詰む)")
    return problems


def init_bootstrap_problems(script):
    """init-bootstrap のスクリプト本文を受け取り問題リストを返す (空なら合格)。"""
    problems = []
    if "PG_VERSION" not in script:
        problems.append(
            "既存 PGDATA を判別する PG_VERSION チェックが無い — これが無いと"
            "既存データの上で initdb を叩き得る")
    for marker in ("initdb", "createdb", "CREATE EXTENSION IF NOT EXISTS vchord"):
        if marker not in script:
            problems.append(f"{marker} が無い (docs の台本と不一致)")
    return problems


def _env_map(container):
    return {e.get("name"): e for e in (container or {}).get("env") or []}


def evaluate(spec):
    """pod template spec を受け取り、更新対策の欠落を問題リストで返す (空なら合格)。"""
    main = container_of(spec, "postgres")
    if main is None:
        return ["main container 'postgres' が無い"]
    boot = container_of(spec, "init-bootstrap")

    problems = []

    # 差分 1: image (main + init-bootstrap の両方が 16.14 系で同一タグ)
    roles = [("main postgres", main), ("init-bootstrap", boot)]
    stale = [r for r, c in roles
             if not (isinstance((c or {}).get("image"), str)
                     and IMAGE_RE.match(c["image"]))]
    if stale:
        problems.append(
            f"image が cloudnative-vectorchord:16.14 系ではない: {stale} "
            f"(現状 main={main.get('image')!r})")
    elif boot is not None and boot["image"] != main["image"]:
        problems.append(
            "init-bootstrap の image を main と同一タグにする — bootstrap だけ"
            "旧いと災害復旧時に旧カタログの PGDATA が作られる")

    # 差分 2: command 明示と args 固定 (#244 再発防止)
    if list(main.get("command") or []) != EXPECTED_COMMAND:
        problems.append(
            'command: ["postgres"] を明示すること (#244: ENTRYPOINT 無しイメージで'
            " args の先頭 -c がコマンド名に解釈され exec 失敗)")
    if list(main.get("args") or []) != EXPECTED_ARGS:
        problems.append(
            f"args は {EXPECTED_ARGS} で固定 — unix_socket_directories=/tmp 等を足すと"
            " livenessProbe/readinessProbe の pg_isready が既定パスを見て失敗する")

    # 差分 3: init-permissions
    ip = container_of(spec, "init-permissions")
    if ip is None:
        problems.append("initContainer init-permissions が無い")
    else:
        if ((ip.get("securityContext") or {}).get("runAsUser") != 0):
            problems.append(
                "init-permissions を securityContext.runAsUser: 0 で動かす "
                "(chown/chmod に root が要る)")
        script = script_of(ip, "sh")
        if script is None:
            problems.append("init-permissions の command が sh -c 形で無い")
        else:
            problems.extend(init_permissions_problems(script))

    # 差分 4: /var/run/postgresql への emptyDir 被せ (docs 原因 B)
    volumes = {v.get("name"): v for v in spec.get("volumes") or []}
    run_vol = volumes.get(RUN_VOLUME)
    if not isinstance(run_vol, dict) or "emptyDir" not in run_vol:
        problems.append(
            f"emptyDir の volume {RUN_VOLUME} が無い — イメージ内の {SOCKET_DIR} は "
            "uid 26 から書けず lock file 作成に失敗する (docs 原因 B)")
    else:
        mounts = {m.get("name"): m.get("mountPath")
                  for m in main.get("volumeMounts") or []}
        if mounts.get(RUN_VOLUME) != SOCKET_DIR:
            problems.append(
                f"{RUN_VOLUME} を main postgres の {SOCKET_DIR} に mount する")

    # 差分 5: init-bootstrap (災害復旧経路)
    if boot is None:
        problems.append(
            "initContainer init-bootstrap が無い — 旧 ENTRYPOINT が暗黙に担っていた"
            "空 PGDATA からの初期化経路が消える (docs 差分 5)")
    else:
        names = [c.get("name") for c in spec.get("initContainers") or []]
        if "init-permissions" in names and \
                names.index("init-bootstrap") < names.index("init-permissions"):
            problems.append(
                "init-bootstrap は init-permissions の後に置く (PGDATA の所有権が先)"
            )
        script = script_of(boot, "bash")
        if script is None:
            problems.append("init-bootstrap の command が bash -c 形で無い")
        else:
            problems.extend(init_bootstrap_problems(script))
        boot_env = _env_map(boot)
        secret = ((boot_env.get("POSTGRES_PASSWORD") or {}).get("valueFrom")
                  or {}).get("secretKeyRef") or {}
        if secret != {"name": SECRET_NAME, "key": "password"}:
            problems.append(
                f"init-bootstrap の POSTGRES_PASSWORD を本番 Secret ({SECRET_NAME}) "
                "の参照にする")
        if (boot_env.get("PGDATA") or {}).get("value") != PGDATA_DIR:
            problems.append(f"init-bootstrap の PGDATA env を {PGDATA_DIR} に揃える")

    return problems


def syntax_ok(shell, script):
    proc = subprocess.run([shell, "-n"], input=script, capture_output=True,
                          text=True, timeout=30)
    return proc.returncode == 0, proc.stderr.strip()


# ---------------------------------------------------------------------------
# 純関数の両方向テスト (合成入力)
# ---------------------------------------------------------------------------


class TestInitPermissionsScript(unittest.TestCase):
    def test_good_form_passes(self):
        self.assertEqual(init_permissions_problems(GOOD_IP_SCRIPT), [])

    def test_chained_chown_and_chmod_is_rejected(self):
        old_form = ("set -eu\n"
                    "chown -R 26:999 /var/lib/postgresql/data && "
                    "chmod 0700 /var/lib/postgresql/data/pgdata\n")
        problems = init_permissions_problems(old_form)
        joined = "; ".join(problems)
        self.assertTrue(any("&&" in p for p in problems), joined)
        self.assertTrue(any("ガード" in p for p in problems), joined)

    def test_unguarded_chmod_is_rejected(self):
        script = ("set -eu\n"
                  "chown -R 26:999 /var/lib/postgresql/data\n"
                  "chmod 0700 /var/lib/postgresql/data/pgdata\n")
        problems = init_permissions_problems(script)
        self.assertEqual(len(problems), 1)
        self.assertIn("ガード", problems[0])

    def test_guard_on_wrong_path_is_rejected(self):
        script = ("set -eu\n"
                  "chown -R 26:999 /var/lib/postgresql/data\n"
                  "if [ -d /tmp/somewhere ]; then\n"
                  "  chmod 0700 /var/lib/postgresql/data/pgdata\n"
                  "fi\n")
        problems = init_permissions_problems(script)
        self.assertTrue(any("ガードの外" in p for p in problems))

    def test_missing_chmod_is_rejected(self):
        problems = init_permissions_problems(
            "set -eu\nchown -R 26:999 /var/lib/postgresql/data\n")
        self.assertTrue(any("chmod 0700" in p for p in problems))

    def test_missing_chown_is_rejected(self):
        problems = init_permissions_problems(GOOD_IP_SCRIPT.replace(
            "chown -R 26:999 ", "chown -R 999:999 "))
        self.assertTrue(any("chown -R 26:999" in p for p in problems))


class TestInitBootstrapScript(unittest.TestCase):
    def test_good_form_passes(self):
        self.assertEqual(init_bootstrap_problems(GOOD_BOOT_SCRIPT), [])

    def test_skip_guard_for_existing_pgdata_is_required(self):
        body = GOOD_BOOT_SCRIPT.split("LOGFILE=", 1)[1]
        problems = init_bootstrap_problems("set -u\nLOGFILE=" + body)
        self.assertTrue(any("PG_VERSION" in p for p in problems))

    def test_each_stage_marker_is_required(self):
        for marker in ("initdb", "createdb",
                       "CREATE EXTENSION IF NOT EXISTS vchord"):
            without = GOOD_BOOT_SCRIPT.replace(marker, "REDACTED")
            self.assertTrue(
                any(marker in p for p in init_bootstrap_problems(without)),
                marker)


class TestEvaluate(unittest.TestCase):
    IMAGE = "ghcr.io/tensorchord/cloudnative-vectorchord:16.14-1.1.1"

    def assert_problem(self, spec, *keywords):
        problems = evaluate(spec)
        joined = "; ".join(problems)
        for kw in keywords:
            self.assertTrue(any(kw in p for p in problems),
                            f"{kw!r} が無い: {joined}")
        return problems

    def test_good_spec_has_no_problems(self):
        self.assertEqual(evaluate(make_good_spec()), [])

    def test_old_16_9_image_is_rejected(self):
        problems = self.assert_problem(
            mutate(lambda s: s["containers"][0].__setitem__(
                "image", "ghcr.io/tensorchord/cloudnative-vectorchord:16.9-0.4.3")),
            "16.14")
        self.assertEqual(len(problems), 1, problems)

    def test_bootstrap_on_different_tag_within_series_is_rejected(self):
        def retag(s):
            s["initContainers"][1]["image"] = \
                s["initContainers"][1]["image"].replace("16.14-1.1.1",
                                                        "16.14-9.9.9")
        self.assert_problem(mutate(retag), "同一タグ")

    def test_missing_command_is_rejected_as_244_shape(self):
        self.assert_problem(mutate(lambda s: s["containers"][0].pop("command")),
                            "#244")

    def test_extra_socket_arg_in_args_is_rejected(self):
        # pg_isready probe が既定パスを見る前提なので args は固定
        self.assert_problem(
            mutate(lambda s: s["containers"][0].__setitem__("args", [
                "-c", "shared_preload_libraries=vchord.so",
                "-c", "unix_socket_directories=/tmp"])),
            "args")

    def test_init_permissions_removed(self):
        self.assert_problem(
            mutate(lambda s: s["initContainers"].pop(0)), "init-permissions")

    def test_init_permissions_without_root_is_rejected(self):
        self.assert_problem(
            mutate(lambda s: s["initContainers"][0].pop("securityContext")),
            "root")

    def test_init_permissions_with_chained_form_is_rejected(self):
        chained = ("set -eu\n"
                   "chown -R 26:999 /var/lib/postgresql/data && "
                   "chmod 0700 /var/lib/postgresql/data/pgdata\n")
        problems = self.assert_problem(
            mutate(lambda s: s["initContainers"][0]["command"].__setitem__(
                2, chained)),
            "&&", "ガード")
        self.assertGreaterEqual(len(problems), 2)

    def test_bootstrap_before_permissions_is_rejected(self):
        self.assert_problem(
            mutate(lambda s: s["initContainers"].reverse()), "後に置く")

    def test_run_volume_removed(self):
        self.assert_problem(
            mutate(lambda s: s.__setitem__(
                "volumes", [v for v in s["volumes"]
                            if v["name"] != RUN_VOLUME])),
            RUN_VOLUME)

    def test_run_volume_mounted_at_wrong_path(self):
        self.assert_problem(
            mutate(lambda s: s["containers"][0]["volumeMounts"][1].__setitem__(
                "mountPath", "/run/postgresql")),
            SOCKET_DIR)

    def test_bootstrap_removed(self):
        problems = self.assert_problem(
            mutate(lambda s: s["initContainers"].pop(1)),
            "init-bootstrap")
        self.assertGreaterEqual(len(problems), 2)  # 災害復旧経路 + image の両方で落ちる

    def test_bootstrap_without_version_check_is_rejected(self):
        self.assert_problem(
            mutate(lambda s: s["initContainers"][1]["command"].__setitem__(
                2, GOOD_BOOT_SCRIPT.replace("PG_VERSION", "MAGIC"))),
            "PG_VERSION")

    def test_bootstrap_with_inline_password_is_rejected(self):
        self.assert_problem(
            mutate(lambda s: s["initContainers"][1]["env"].__setitem__(
                0, {"name": "POSTGRES_PASSWORD", "value": "hunter2"})),
            SECRET_NAME)


# ---------------------------------------------------------------------------
# 実リポジトリのマニフェストに対する検査
# ---------------------------------------------------------------------------


class TestRealRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = load_docs()
        dep = deployment_of(cls.docs)
        assert dep is not None, "Deployment immich-postgres が無い"
        cls.dep = dep
        cls.spec = dep["spec"]["template"]["spec"]

    def test_measures_are_all_in_place(self):
        self.assertEqual(evaluate(self.spec), [],
                         "\n".join(evaluate(self.spec)))

    def test_embedded_scripts_are_syntactically_valid(self):
        ip = container_of(self.spec, "init-permissions")
        ok, err = syntax_ok("sh", script_of(ip, "sh"))
        self.assertTrue(ok, f"init-permissions が sh -n 失敗: {err}")
        boot = container_of(self.spec, "init-bootstrap")
        ok, err = syntax_ok("bash", script_of(boot, "bash"))
        self.assertTrue(ok, f"init-bootstrap が bash -n 失敗: {err}")

    def test_regression_pins_that_predate_the_upgrade(self):
        # 更新差分に紛れて壊れてほしくない既存の性質
        self.assertEqual(self.dep["metadata"]["namespace"], "immich")
        self.assertEqual(self.dep["spec"]["strategy"]["type"], "Recreate")
        self.assertEqual(self.spec["securityContext"]["fsGroup"], 999)
        volumes = {v["name"]: v for v in self.spec["volumes"]}
        claim = volumes["data"]["persistentVolumeClaim"]
        self.assertEqual(claim["claimName"], "immich-postgres-data")
        pvc = next(d for d in self.docs if d.get("kind") == "PersistentVolumeClaim")
        self.assertEqual(
            pvc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"],
            "Prune=false")

    def test_probes_still_use_default_socket_path(self):
        # postgres-run emptyDir 方式の前提: pg_isready は -h 無し (= 既定の
        # /var/run/postgresql) で叩く
        main = container_of(self.spec, "postgres")
        for probe in ("livenessProbe", "readinessProbe"):
            cmd = main[probe]["exec"]["command"]
            self.assertIn("pg_isready", cmd)
            self.assertNotIn("-h", cmd)


if __name__ == "__main__":
    unittest.main()

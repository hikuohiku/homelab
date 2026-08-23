"""ops/tools/syncthing_acceptance.py の検査ロジックを固定する (P-0163)。

リポジトリルートから `python3 -m unittest ops.tests.test_syncthing_acceptance`。
**一切ネットワークに出ない**: HTTP/TCP プローブは run_checks の注入ポイントに
FakeFetcher 型の fake を渡し、REST は SyncthingApi と同じ request() シグネチャの
fake を渡す。dict に無い URL へのアクセスは即座に失敗するので、実装が勝手に
別のエンドポイントを叩いて通ってしまうことも防ぐ (test_version_watch.py 流儀)。

device ID 導出のゴールデンベクトルは syncthing 本家のテスト
(lib/protocol/deviceid_test.go, Mozilla-2.0) から取った。本家が「この文字列は
この device ID になる」と固定しているものをそのまま移植側の仕様として使う。
fixture 証明書は公開ルート CA (Amazon Root CA 3) の PEM で、真正な自己署名
証明書として ssl.PEM_cert_to_DER_cert → sha256 の経路を実入力で通すためのもの。
"""

import json
import base64
import hashlib
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path

from ops.tools import syncthing_acceptance as sa

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_CERT = FIXTURES / "syncthing-fixture-cert.pem"

# syncthing lib/protocol/deviceid_test.go の formatted / formatCases より。
# 52 文字の base32 (旧形式) と、それに検査数字が付いた正規形。
B52_VEC = "P56IOI7MZJNU2IQGDREYDM2MGTMGL3BXNPQ6W5BTBBZ4TJXZWICQ"
CANON_VEC = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"

# fixture 証明書の DER を sha256 → base32 (pad 除去) した値と正規形 device ID。
# fixture の生成時に openssl 系プリミティブではなく python ssl/hashlib/base64 の
# 独立経路で計算し、下の derive テストが「自分自身の実装の焼き直し」にならない
# ようにしている (luhn 部は上の本家ベクトルが担保する)
FIXTURE_B52 = "DDHGZ7T36FHGBMXDI64N72DIZMY5ALV3HLNCOFLJ6UBUHNDNWOSA"
FIXTURE_CANON = "DDHGZ7T-36FHGBG-MXDI64N-72DIZMI-Y5ALV3H-LNCOFL2-J6UBUHN-DNWOSAC"

# 別個体の peer を表す正規形 device ID (形式のみ使用)
PEER_ID = "KHQF2L7-2IHJCQ2-CETV3FH-DGKKLLJ-TV4QLJA-J3GSZOH-V4HCL9D-GMCAWAP"


def config_xml(self_id=CANON_VEC, folders=None):
    """合成 config 断片 (DoD 4 の fixture)。syncthing 2.x の実際の形に揃える。"""
    if folders is None:
        folders = [
            ("photos", "/var/syncthing/photos"),
            ("docs", "/var/lib/syncthing/docs"),  # 旧 LXC 101 のパスが残った例
        ]
    folder_blocks = "\n".join(
        f'''    <folder id="{fid}" label="" path="{path}" type="sendreceive"
            rescanIntervalS="3600" fsWatcherEnabled="true" ignorePerms="false">
        <filesystemType>basic</filesystemType>
        <device id="{self_id}"></device>
    </folder>''' for fid, path in folders)
    return f'''<?xml version="1.0"?>
<configuration version="37">
{folder_blocks}
    <device id="{self_id}" name="old-lxc101" compression="metadata"
        introducer="false">
        <address>dynamic</address>
        <paused>false</paused>
        <autoAcceptFolders>false</autoAcceptFolders>
    </device>
    <device id="{PEER_ID}" name="laptop" compression="metadata"
        introducer="false">
        <address>tcp://192.168.1.10:22000</address>
    </device>
    <gui enabled="true" tls="false" debugging="false">
        <address>127.0.0.1:8384</address>
        <apikey>apikey-fixture-0123456789abcdef</apikey>
        <theme>default</theme>
    </gui>
</configuration>
'''


class FakeFetcher:
    """URL -> (status, body) の辞書。network-free の要。"""

    def __init__(self, responses):
        self.responses = responses

    def __call__(self, url):
        if url not in self.responses:
            raise AssertionError("fixture に無い URL へのアクセス: {}".format(url))
        return self.responses[url]


def refusing(url):
    raise ConnectionRefusedError(f"refused: {url}")


class TestLuhn32(unittest.TestCase):
    def test_known_chunks_from_upstream_vector(self):
        # 正規形 CANON_VEC の各グループ末尾が検査数字。直前 13 文字との対応を
        # 本家アルゴリズムが再現すること
        self.assertEqual(sa.luhn32(B52_VEC[0:13]), "Y")
        self.assertEqual(sa.luhn32(B52_VEC[13:26]), "I")
        self.assertEqual(sa.luhn32(B52_VEC[26:39]), "M")
        self.assertEqual(sa.luhn32(B52_VEC[39:52]), "2")

    def test_rejects_non_alphabet(self):
        with self.assertRaises(ValueError):
            sa.luhn32("P56IOI7MZJNU0")  # 0 は base32 に無い


class TestDeviceIdChain(unittest.TestCase):
    def test_golden_vector_end_to_end(self):
        self.assertEqual(sa.chunkify(sa.luhnify(B52_VEC)), CANON_VEC)

    def test_luhnify_roundtrip(self):
        for core in (B52_VEC, "A" * 52, "7" * 52, "234567" * 8 + "ABCD"):
            with self.subTest(core=core[:8]):
                styled = sa.luhnify(core)
                self.assertEqual(len(styled), 56)
                self.assertEqual(sa.unluhnify(styled), core)

    def test_unluhnify_detects_broken_check_digit(self):
        styled = list(sa.luhnify(B52_VEC))
        styled[-1] = "A" if styled[-1] != "A" else "B"
        with self.assertRaises(ValueError):
            sa.unluhnify("".join(styled))

    def test_luhnify_wrong_length_raises(self):
        with self.assertRaises(ValueError):
            sa.luhnify("SHORT")


class TestCanonicalDeviceId(unittest.TestCase):
    def test_accepts_canonical_as_is(self):
        self.assertEqual(sa.canonical_device_id(CANON_VEC), CANON_VEC)

    def test_normalizes_case_dashes_spaces_and_typos(self):
        # 大小文字混在・区切り無し・空白区切り・タイプ修正 (0→O,1→I,8→B) は
        # すべて本家 UnmarshalText が受け入れる形。そのうち代表的なものを固定
        typo_form = "P561017MZJNU2YIQGDREYDM2MGTIMGL3BXNPQ6W5BMT88Z4TJXZWICQ2"
        variants = [
            CANON_VEC.lower(),
            CANON_VEC.replace("-", ""),
            " ".join([CANON_VEC[i:i + 7] for i in range(0, len(CANON_VEC), 8)]),
            B52_VEC,  # 旧形式 52 文字 (検査数字なし) も受け入れて拡張する
            typo_form,
        ]
        for v in variants:
            with self.subTest(v=v):
                self.assertEqual(sa.canonical_device_id(v), CANON_VEC)

    def test_rejects_bad_lengths_and_alphabet(self):
        bad = [
            "",                       # 空
            "P56IOI7",                # 短すぎ
            CANON_VEC[:-1],           # 長さ 62 (正規化後 55)
            CANON_VEC + "AAAA",       # 長すぎ
            "9" + B52_VEC[1:],        # alphabet 外
        ]
        for v in bad:
            with self.subTest(v=v):
                with self.assertRaises(ValueError):
                    sa.canonical_device_id(v)


class TestDeriveDeviceId(unittest.TestCase):
    def test_fixture_cert_matches_independent_ground_truth(self):
        pem = FIXTURE_CERT.read_text(encoding="ascii")
        derived = sa.derive_device_id(pem)
        # luhn 部を除いた base32 までが独立算出した値と一致し、
        # 正規形は luhn(本家ベクトルで検証済み) を通ったものと一致する
        der = ssl.PEM_cert_to_DER_cert(pem)
        b52 = base64.b32encode(hashlib.sha256(der).digest()).decode().rstrip("=")
        self.assertEqual(b52, FIXTURE_B52)
        self.assertEqual(derived, FIXTURE_CANON)
        ok, normalized = sa.validate_device_id(derived)
        self.assertTrue(ok)
        self.assertEqual(normalized, FIXTURE_CANON)

    def test_accepts_bytes_input(self):
        self.assertEqual(sa.derive_device_id(FIXTURE_CERT.read_bytes()),
                         FIXTURE_CANON)

    def test_garbage_pem_raises_valueerror(self):
        with self.assertRaises(ValueError):
            sa.derive_device_id("not a pem at all")


class TestParseConfig(unittest.TestCase):
    def test_extracts_folders_devices_and_apikey(self):
        info = sa.parse_config(config_xml())
        self.assertEqual([f["id"] for f in info["folders"]], ["photos", "docs"])
        self.assertEqual(info["folders"][1]["path"], "/var/lib/syncthing/docs")
        self.assertIn(CANON_VEC, info["device_ids"])
        self.assertIn(PEER_ID, info["device_ids"])
        self.assertEqual(info["api_key"], "apikey-fixture-0123456789abcdef")

    def test_broken_xml_raises(self):
        with self.assertRaises(ValueError):
            sa.parse_config("<configuration><folder>")


class TestEvaluateFolderPaths(unittest.TestCase):
    ROOT = "/var/syncthing"

    def test_ok_paths_pass(self):
        folders = [{"id": "a", "path": "/var/syncthing/a"},
                   {"id": "b", "path": "/var/syncthing/b/sub/deep"}]
        problems, n = sa.evaluate_folder_paths(folders, self.ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(n, 2)

    def test_stale_old_lxc_path_is_flagged(self):
        folders = [{"id": "docs", "path": "/var/lib/syncthing/docs"}]
        problems, _ = sa.evaluate_folder_paths(folders, self.ROOT)
        self.assertEqual(len(problems), 1)
        self.assertIn("/var/lib/syncthing/docs", problems[0])
        self.assertIn("配下にない", problems[0])

    def test_no_folders_is_a_problem(self):
        problems, _ = sa.evaluate_folder_paths([], self.ROOT)
        self.assertTrue(any("1 件も無い" in p for p in problems))

    def test_duplicate_ids_relative_path_and_missing_fields(self):
        folders = [
            {"id": "a", "path": "/var/syncthing/a"},
            {"id": "a", "path": "/var/syncthing/copy"},
            {"id": None, "path": "relative/path"},
            {"id": "empty", "path": ""},
        ]
        problems, _ = sa.evaluate_folder_paths(folders, self.ROOT)
        joined = "; ".join(problems)
        self.assertIn("重複", joined)
        self.assertIn("相対パス", joined)
        self.assertIn("path が未定義", joined)


class TestResticCoverage(unittest.TestCase):
    def test_real_manifest_in_repo_is_covered(self):
        # 実マニフェストに対する検査なので、cronjob 側の意図しない変更
        # (対象縮小・危険な exclude 追加) をこのテストが検知する
        text = sa.DEFAULT_RESTIC_MANIFEST.read_text(encoding="utf-8")
        problems, parsed = sa.evaluate_restic_coverage(text)
        self.assertEqual(problems, [])
        self.assertTrue(parsed["found"])

    def test_missing_backup_command(self):
        problems, parsed = sa.evaluate_restic_coverage("kind: CronJob\n")
        self.assertFalse(parsed["found"])
        self.assertTrue(problems)

    def test_unexpected_exclude_is_flagged(self):
        text = (
            "                  restic backup \\\n"
            "                    --exclude=/mnt/syncthing-data/config/index-v2 \\\n"
            "                    --exclude=/mnt/syncthing-data/photos \\\n"
            "                    /mnt/syncthing-data\n")
        problems, parsed = sa.evaluate_restic_coverage(text)
        self.assertTrue(any("photos" in p for p in problems))
        self.assertIn("/mnt/syncthing-data/photos", parsed["excludes"])

    def test_narrowed_target_is_flagged(self):
        text = (
            "                  restic backup \\\n"
            "                    /mnt/syncthing-data/config\n")
        problems, _ = sa.evaluate_restic_coverage(text)
        self.assertTrue(any("対象に /mnt/syncthing-data が無い" in p for p in problems))

    def test_safe_excludes_pass(self):
        text = (
            "                  restic backup \\\n"
            "                    --exclude=/mnt/syncthing-data/config/index-v2 \\\n"
            "                    --exclude=/mnt/syncthing-data/config/syncthing.lock \\\n"
            "                    /mnt/syncthing-data\n")
        problems, _ = sa.evaluate_restic_coverage(text)
        self.assertEqual(problems, [])


def make_data_dir(tmp, *, layout="flat", self_id=CANON_VEC, folders=None,
                  include_all=True):
    """check 用の合成データディレクトリを作る。

    fixture 証明書を cert.pem として置くので、導出 device ID は FIXTURE_CANON。
    self_id 既定値はそれと異なる (取り違え検知を試すため) ので、一致させたい
    場合は明示的に FIXTURE_CANON を渡すこと。
    """
    base = tmp / "data" if layout == "flat" else tmp / "data" / "config"
    base.mkdir(parents=True, exist_ok=True)
    if include_all:
        (base / "cert.pem").write_text(FIXTURE_CERT.read_text(encoding="ascii"))
        (base / "key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n")
        (base / "config.xml").write_text(
            config_xml(self_id=self_id, folders=folders), encoding="utf-8")
    return tmp / "data"


class TestRunChecks(unittest.TestCase):
    GUI_URL = "http://gui.test:8384"

    def run_checks(self, data_dir, http_get=None, tcp_connect=None,
                   **kw):
        return sa.run_checks(
            data_dir=data_dir,
            gui_url=self.GUI_URL,
            restic_manifest=sa.DEFAULT_RESTIC_MANIFEST,
            http_get=http_get or FakeFetcher(
                {self.GUI_URL.rstrip("/") + "/rest/noauth/health":
                 (200, '{"status":"OK"}')}),
            tcp_connect=tcp_connect or (lambda addr: True),
            **kw)

    def by_name(self, results):
        return {r["name"]: r for r in results}

    def test_happy_flat_layout_all_green_strict_ok(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), self_id=FIXTURE_CANON,
                                     folders=[("photos", "/var/syncthing/photos")])
            results = self.run_checks(data_dir)
            statuses = {r["name"]: r["status"] for r in results}
            self.assertEqual(
                statuses,
                {"identity-files": sa.PASS, "device-id-format": sa.PASS,
                 "self-device-declared": sa.PASS, "folder-paths": sa.PASS,
                 "pvc-rw": sa.PASS, "restic-coverage": sa.PASS,
                 "gui-health": sa.PASS, "tailnet-sync": sa.PASS})
            self.assertEqual(sa.exit_code(results, strict=True), 0)
            self.assertIn(str(data_dir), self.by_name(results)["identity-files"]["detail"])

    def test_nested_layout_also_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), layout="nested",
                                     self_id=FIXTURE_CANON,
                                     folders=[("photos", "/var/syncthing/photos")])
            results = self.run_checks(data_dir)
            detail = self.by_name(results)["identity-files"]["detail"]
            self.assertIn("layout=nested", detail)
            self.assertEqual(sa.exit_code(results), 0)

    def test_cert_config_mismatch_fails_required(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td))  # config は CANON_VEC を宣言
            results = self.run_checks(data_dir)
            self.assertEqual(
                self.by_name(results)["self-device-declared"]["status"], sa.FAIL)
            self.assertEqual(sa.exit_code(results), 1)

    def test_stale_folder_path_fails_with_hint(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), self_id=FIXTURE_CANON)
            results = self.run_checks(data_dir)
            failed = self.by_name(results)["folder-paths"]
            self.assertEqual(failed["status"], sa.FAIL)
            self.assertIn("/var/lib/syncthing/docs", failed["detail"])
            self.assertEqual(sa.exit_code(results), 1)

    def test_unreachable_probes_are_unknown_not_fail(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), self_id=FIXTURE_CANON,
                                     folders=[("photos", "/var/syncthing/p")])
            results = self.run_checks(data_dir, http_get=refusing,
                                      tcp_connect=refusing)
            names = self.by_name(results)
            self.assertEqual(names["gui-health"]["status"], sa.UNKNOWN)
            self.assertEqual(names["tailnet-sync"]["status"], sa.UNKNOWN)
            # 不明は単独では落とさない (--strict では落ちる)
            self.assertEqual(sa.exit_code(results, strict=False), 0)
            self.assertEqual(sa.exit_code(results, strict=True), 1)

    def test_health_endpoint_404_is_fail_not_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), self_id=FIXTURE_CANON,
                                     folders=[("photos", "/var/syncthing/p")])

            def http_404(url):
                raise urllib.error.HTTPError(url, 404, "nope", {},
                                             io.BytesIO(b""))

            # HTTPError の GC タイミングで ResourceWarning が出ることがあるので
            # この検証では無視する
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                results = self.run_checks(data_dir, http_get=http_404)
            self.assertEqual(self.by_name(results)["gui-health"]["status"], sa.FAIL)
            self.assertEqual(sa.exit_code(results), 1)

    def test_empty_data_dir_fails_identity_and_dependents_are_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), include_all=False)
            results = self.run_checks(data_dir)
            names = self.by_name(results)
            self.assertEqual(names["identity-files"]["status"], sa.FAIL)
            for dependent in ("device-id-format", "self-device-declared",
                              "folder-paths"):
                self.assertEqual(names[dependent]["status"], sa.UNKNOWN)
            # 書き込み自体はできるので pvc-rw は通る
            self.assertEqual(names["pvc-rw"]["status"], sa.PASS)
            self.assertEqual(sa.exit_code(results), 1)

    def test_probe_never_hits_other_urls(self):
        # FakeFetcher に無い URL へのアクセスは AssertionError になる。
        # add() がこれを「不明」に変換するのはプローブ失敗の扱いとして正しい
        # (必須検査が通っている前提の任意検査なので全体は落とさない)
        with tempfile.TemporaryDirectory() as td:
            data_dir = make_data_dir(Path(td), self_id=FIXTURE_CANON,
                                     folders=[("photos", "/var/syncthing/p")])
            results = sa.run_checks(
                data_dir=data_dir, gui_url=self.GUI_URL,
                restic_manifest=sa.DEFAULT_RESTIC_MANIFEST,
                http_get=FakeFetcher({}), tcp_connect=lambda addr: True)
            self.assertEqual(self.by_name(results)["gui-health"]["status"],
                             sa.UNKNOWN)


class RecordingApi:
    """SyncthingApi と同じ request() 面を持つ fake。scripted 応答を返す。"""

    def __init__(self, status_body=None, fail=False):
        self.calls = []
        self.status_body = status_body or {
            "state": "idle", "globalBytes": 64, "invalid": ""}
        self.fail = fail

    def request(self, method, path, payload=None):
        self.calls.append((method, path.split("?")[0]))
        if self.fail:
            raise sa.SyncthingApiError(f"{method} {path} -> boom")
        if method == "GET" and path.startswith("/rest/db/status"):
            body = dict(self.status_body)
            if callable(body):
                body = body()
            return 200, body
        return 200, {}

    def called(self, method, prefix):
        return any(m == method and p.startswith(prefix) for m, p in self.calls)


class NoSleep:
    def __init__(self):
        self.total = 0.0

    def __call__(self, s):
        self.total += s


class StFolderMkdirApi(RecordingApi):
    """POST /rest/db/scan を受けたら実物同様に .stfolder を掘る fake。

    本物の syncthing はフォルダ登録後の初回 scan で folder path 直下に
    .stfolder を作る (.stfolder が無いとフォルダが invalid 扱いになるため、
    rescan 収束時点で確実に存在する)。素の RecordingApi はファイルシステムに
    触れないためこの挙動を再現せず、cleanup が .stfolder 残置で落ちる
    本番限定のバグを捉えられなかった (2026-08-23 レビューで合成環境実測)。
    """

    def __init__(self, local_dir, status_body=None):
        super().__init__(status_body=status_body)
        self.local_dir = Path(local_dir)
        self.stfolder_created = False

    def request(self, method, path, payload=None):
        if method == "POST" and path.split("?")[0] == "/rest/db/scan":
            (self.local_dir / ".stfolder").mkdir(parents=True, exist_ok=True)
            self.stfolder_created = True
        return super().request(method, path, payload)


class TestRunExercise(unittest.TestCase):
    def run_exercise(self, api, tmp, **kw):
        kw.setdefault("max_polls", 5)
        kw.setdefault("sleep", NoSleep())
        return sa.run_exercise(api, data_dir=tmp, self_device_id=FIXTURE_CANON,
                               **kw)

    def by_name(self, results):
        return {r["name"]: r for r in results}

    def test_happy_flow_registers_scans_verifies_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            api = RecordingApi({"state": "idle", "globalBytes": 64, "invalid": ""})
            results = self.run_exercise(api, tmp)
            statuses = {r["name"]: r["status"] for r in results}
            self.assertEqual(statuses, {
                "exercise-write": sa.PASS, "exercise-folder-add": sa.PASS,
                "exercise-rescan": sa.PASS, "exercise-readback": sa.PASS,
                "exercise-restic-covered": sa.PASS, "exercise-cleanup": sa.PASS})
            self.assertEqual(sa.exercise_exit_code(results), 0)
            self.assertTrue(api.called("PUT", "/rest/config/folders/acceptance-dummy"))
            self.assertTrue(api.called("POST", "/rest/db/scan"))
            self.assertTrue(api.called("DELETE", "/rest/config/folders/acceptance-dummy"))
            # 後始末でダミーディレクトリが消えている
            self.assertFalse((tmp / "acceptance-dummy").exists())

    def test_rescan_timeout_is_unknown_and_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            api = RecordingApi({"state": "scanning", "globalBytes": 0,
                                "invalid": ""})
            results = self.run_exercise(api, tmp)
            names = self.by_name(results)
            self.assertEqual(names["exercise-rescan"]["status"], sa.UNKNOWN)
            self.assertEqual(sa.exercise_exit_code(results), 1)
            self.assertTrue(api.called("DELETE", "/rest/config/folders/acceptance-dummy"))
            self.assertFalse((tmp / "acceptance-dummy").exists())

    def test_invalid_folder_state_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            api = RecordingApi({"state": "idle", "globalBytes": 64,
                                "invalid": "folder path missing"})
            results = self.run_exercise(api, tmp)
            names = self.by_name(results)
            self.assertEqual(names["exercise-rescan"]["status"], sa.FAIL)
            self.assertIn("folder path missing", names["exercise-rescan"]["detail"])
            # 収束していないので読み戻しは判定しない (偽の緑を作らない)
            self.assertEqual(names["exercise-readback"]["status"], sa.UNKNOWN)

    def test_api_down_degrades_without_crash_and_reports_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            api = RecordingApi(fail=True)
            results = self.run_exercise(api, tmp)
            names = self.by_name(results)
            self.assertEqual(names["exercise-write"]["status"], sa.PASS)
            self.assertEqual(names["exercise-folder-add"]["status"], sa.UNKNOWN)
            self.assertNotIn("exercise-rescan", names)
            self.assertEqual(names["exercise-readback"]["status"], sa.UNKNOWN)
            self.assertEqual(names["exercise-cleanup"]["status"], sa.UNKNOWN)
            self.assertEqual(sa.exercise_exit_code(results), 1)
            self.assertFalse((tmp / "acceptance-dummy").exists())

    def test_scan_creates_stfolder_and_cleanup_still_passes(self):
        # 実機では初回 scan で .stfolder が掘られる。それを再現しても
        # 演習全体 (cleanup 含む) が合格し、ダミーディレクトリは
        # .stfolder ごと跡形なく消えること。stfolder_created の assert で
        # 「バグの再現条件が成立していた」ことを保証する (空振りテスト防止)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            local_dir = tmp / "acceptance-dummy"
            api = StFolderMkdirApi(local_dir,
                                   {"state": "idle", "globalBytes": 64,
                                    "invalid": ""})
            results = self.run_exercise(api, tmp)
            self.assertTrue(api.stfolder_created)
            statuses = {r["name"]: r["status"] for r in results}
            self.assertEqual(statuses["exercise-cleanup"], sa.PASS)
            self.assertEqual(sa.exercise_exit_code(results), 0)
            self.assertFalse(local_dir.exists())

    def test_rescan_timeout_with_stfolder_still_cleans_up_completely(self):
        # 収束失敗 (exit 1) の経路でも .stfolder 残置で cleanup が落ちないこと。
        # 不合格時に手動削除の残骸を残さないのが目的
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            local_dir = tmp / "acceptance-dummy"
            api = StFolderMkdirApi(local_dir,
                                   {"state": "scanning", "globalBytes": 0,
                                    "invalid": ""})
            results = self.run_exercise(api, tmp)
            names = self.by_name(results)
            self.assertTrue(api.stfolder_created)
            self.assertEqual(names["exercise-rescan"]["status"], sa.UNKNOWN)
            self.assertEqual(names["exercise-cleanup"]["status"], sa.PASS)
            self.assertEqual(sa.exercise_exit_code(results), 1)
            self.assertFalse(local_dir.exists())


class TestRenderAndExitCodes(unittest.TestCase):
    def test_render_marks_required_and_verdicts(self):
        results = [
            sa.make_result("ok-check", True, sa.PASS, "fine"),
            sa.make_result("bad-check", True, sa.FAIL, "broken"),
            sa.make_result("maybe", False, sa.UNKNOWN, "cannot reach"),
        ]
        text = sa.render(results, "title")
        self.assertIn("* bad-check", text)
        self.assertNotIn("* maybe", text)
        self.assertIn("判定: 不合格", text)

        all_unknown = [sa.make_result("m", True, sa.UNKNOWN, "?")]
        self.assertIn("--strict", sa.render(all_unknown, "t"))

        all_pass = [sa.make_result("m", True, sa.PASS, "!")]
        self.assertIn("判定: 合格", sa.render(all_pass, "t"))

    def test_exit_code_matrix(self):
        def rs(**st):
            table = {"p": sa.PASS, "f": sa.FAIL, "u": sa.UNKNOWN}
            out = []
            for i, s in enumerate(st.pop("statuses")):
                out.append(sa.make_result(f"c{i}", st.pop("required", True),
                                          table[s], ""))
            assert not st, st
            return out

        self.assertEqual(sa.exit_code(rs(statuses="pp")), 0)
        self.assertEqual(sa.exit_code(rs(statuses="pf")), 1)
        self.assertEqual(sa.exit_code(rs(statuses="pu")), 0)
        self.assertEqual(sa.exit_code(rs(statuses="pu"), strict=True), 1)
        # 任意検査の fail でも落とす (応答異常は確定的な否定情報)
        mixed = [sa.make_result("req", True, sa.PASS, ""),
                 sa.make_result("opt", False, sa.FAIL, "")]
        self.assertEqual(sa.exit_code(mixed), 1)


if __name__ == "__main__":
    unittest.main()

"""apps/wish-seeds/run_ask.py (P-0192 の問いかけ送信 Job) の契約を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_wish_seeds_job`。

固定する契約:

- ConfigMap 内の wish_seeds.py コピーは ops/tools/wish_seeds.py と同一内容
  (手動同期コピーの食い違いを機械で落とす)
- **追加送信なし** (spec 予算規則) の 3 層歯止め:
  証跡が main / project ブランチのどこかにあれば送らない (skip)。
  pending マーカーだけがあるなら送らないで騒ぐ (abort。証跡無しの黙認は
  DoD 未達の隠蔽)。書き込み先ブランチが無ければ送らない (abort)
- 送信は sendMessage 1 通のみ。pending は送信直前に書く (クラッシュ後の再実行で
  二重送信しないため)。証跡は Telegram 応答の message_id を持つ (verify 2 の形)
- Contents API への PUT は branch 指定・base64 content・新規作成 (sha 無し)
- Job manifest は Force=true,Replace=true (CHARTER §4) と Secret 参照を持ち、
  ttlSecondsAfterFinished を持たない (完了後 selfHeal 再作成ループの防止)

一切ネットワークに出ない。GitHub Contents API は URL 辞書の Fake、Telegram 送信は
send_telegram の差し替え (test_wish_seeds.py 流儀)。
"""

import base64
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
APP_DIR = REPO / "apps" / "wish-seeds"

SPEC = importlib.util.spec_from_file_location("run_ask", APP_DIR / "run_ask.py")
ra = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ra)

GITHUB_API = ra.GITHUB_API
REPO_SLASH = "hikuohiku/homelab"
BRANCH = "project/p-0192"
EVIDENCE = "ops/projects/logs/P-0192/ask-evidence.json"
PENDING = "ops/projects/logs/P-0192/ask-pending.json"


def contents_url(path):
    return "{}/repos/{}/contents/{}".format(GITHUB_API, REPO_SLASH, path)


def branches_url(branch):
    return "{}/repos/{}/branches/{}".format(GITHUB_API, REPO_SLASH, branch)


class FakeResponse:
    def __init__(self, status, payload):
        self._payload = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


class FakeGitHub:
    """(method, query を除いた URL) → (status, payload) の辞書で API を模す。

    GET contents は ?ref= を解釈し、payload["_exists_refs"] に載っている ref 上で
    200・それ以外は 404 を返す。dict に無い URL へのアクセスは即座に失敗するので、
    実装が勝手に別エンドポイントを叩いて通ることも防ぐ (test_version_watch.py 流儀)。
    PUT の body は self.puts に蓄積する。
    """

    def __init__(self, routes):
        self.routes = routes
        self.puts = []
        self.calls = []

    def urlopen(self, request, timeout=None):
        url = request.full_url
        key = (request.get_method(), url.split("?")[0])
        self.calls.append(key)
        if key not in self.routes:
            raise AssertionError("想定外のリクエスト: {}".format(url))
        status, payload = self.routes[key]
        if request.get_method() == "GET" and "ref=" in url:
            ref = url.split("ref=")[1].split("&")[0]
            exists = (payload or {}).get("_exists_refs") or []
            return FakeResponse(200 if ref in exists else 404,
                                {"sha": "abc"} if ref in exists else {})
        if request.get_method() == "PUT":
            self.puts.append(json.loads(request.data.decode()))
        return FakeResponse(status, payload)


ENV_OK = {
    "GITHUB_REPO": REPO_SLASH,
    "AUTOPILOT_GITHUB_TOKEN": "gtok",
    "TELEGRAM_BOT_TOKEN": "ttok",
    "TELEGRAM_ALLOWED_USER_ID": "42",
}


class TestCopySync(unittest.TestCase):
    def test_configmap_copy_is_identical_to_ops_tools(self):
        copied = (APP_DIR / "wish_seeds.py").read_bytes()
        original = (REPO / "ops" / "tools" / "wish_seeds.py").read_bytes()
        self.assertEqual(copied, original)


class TestDecideSend(unittest.TestCase):
    """送ってよいかの 3 層歯止め。refs は [main, 書き込み先] の順で渡す。"""

    def decide(self, main_ev=(), branch_ev=(), branch_pending=(),
               branch_alive=True):
        routes = {
            ("GET", contents_url(EVIDENCE)):
                (200, {"_exists_refs": [*main_ev, *branch_ev]}),
            ("GET", contents_url(PENDING)):
                (200, {"_exists_refs": list(branch_pending)}),
            ("GET", branches_url(BRANCH)): ((200, {}) if branch_alive else (404, {})),
        }
        gh = FakeGitHub(routes)
        decision, detail = ra.decide_send(
            REPO_SLASH, ["main", BRANCH], EVIDENCE, PENDING, "tok",
            urlopen=gh.urlopen,
        )
        return decision, detail

    def test_nothing_anywhere_and_branch_alive_sends(self):
        decision, _ = self.decide()
        self.assertEqual(decision, "send")

    def test_evidence_on_main_skips_without_sending(self):
        decision, detail = self.decide(main_ev=["main"])
        self.assertEqual(decision, "skip")
        self.assertIn("main", detail)

    def test_evidence_on_project_branch_skips(self):
        decision, _ = self.decide(branch_ev=[BRANCH])
        self.assertEqual(decision, "skip")

    def test_pending_without_evidence_aborts_loudly(self):
        decision, detail = self.decide(branch_pending=[BRANCH])
        self.assertEqual(decision, "abort")
        self.assertIn("二重送信", detail)
        self.assertIn("pending", detail)

    def test_missing_write_branch_refuses_to_send(self):
        decision, detail = self.decide(branch_alive=False)
        self.assertEqual(decision, "abort")
        self.assertIn("記録できる保証", detail)

    def test_refs_order_makes_main_checked_first(self):
        """main の証跡を見つけたら project ブランチは見に行かない (早期 skip)。"""
        routes = {
            ("GET", contents_url(EVIDENCE)):
                (200, {"_exists_refs": ["main"]}),
            ("GET", contents_url(PENDING)): (404, {}),
            ("GET", branches_url(BRANCH)): (200, {}),
        }
        gh = FakeGitHub(routes)
        decision, _ = ra.decide_send(
            REPO_SLASH, ["main", BRANCH], EVIDENCE, PENDING, "tok",
            urlopen=gh.urlopen,
        )
        self.assertEqual(decision, "skip")
        # EVIDENCE(main) の次は PENDING(main) — ブランチ側・branches API には行かない
        checked = [c for c in gh.calls if c[0] == "GET"]
        self.assertIn(("GET", contents_url(EVIDENCE)), checked)
        self.assertNotIn(("GET", branches_url(BRANCH)), checked)


class TestPutFile(unittest.TestCase):
    def test_put_creates_new_file_on_target_branch_without_sha(self):
        routes = {
            ("GET", contents_url(EVIDENCE)): (200, {"_exists_refs": []}),
            ("PUT", contents_url(EVIDENCE)): (201, {"commit": {"sha": "f"}}),
        }
        gh = FakeGitHub(routes)
        ra.put_file(REPO_SLASH, BRANCH, EVIDENCE, "tok",
                    b"x", "msg", urlopen=gh.urlopen)
        put_body = gh.puts[0]
        self.assertEqual(put_body["branch"], BRANCH)
        self.assertEqual(base64.b64decode(put_body["content"]), b"x")
        self.assertEqual(put_body["message"], "msg")
        self.assertNotIn("sha", put_body)

    def test_put_updates_existing_file_with_sha(self):
        routes = {
            ("GET", contents_url(EVIDENCE)): (200, {"_exists_refs": [BRANCH],
                                                    "sha": "abc"}),
            ("PUT", contents_url(EVIDENCE)): (200, {"commit": {"sha": "g"}}),
        }
        gh = FakeGitHub(routes)
        ra.put_file(REPO_SLASH, BRANCH, EVIDENCE, "tok",
                    b"y", "msg2", urlopen=gh.urlopen)
        self.assertEqual(gh.puts[0]["sha"], "abc")

    def test_put_failure_raises_instead_of_passing_quietly(self):
        routes = {
            ("GET", contents_url(EVIDENCE)): (200, {"_exists_refs": []}),
            ("PUT", contents_url(EVIDENCE)): (409, {"message": "conflict"}),
        }
        gh = FakeGitHub(routes)
        with self.assertRaises(RuntimeError):
            ra.put_file(REPO_SLASH, BRANCH, EVIDENCE, "tok",
                        b"x", "msg", urlopen=gh.urlopen)


class TestMainEndToEnd(unittest.TestCase):
    """本物の main() を実 HTTP シーケンス (FakeGitHub × 本物 urllib.Request) で通す。"""

    def setUp(self):
        self.routes = {
            ("GET", contents_url(EVIDENCE)): (200, {"_exists_refs": []}),
            ("GET", contents_url(PENDING)): (200, {"_exists_refs": []}),
            ("GET", branches_url(BRANCH)): (200, {}),
            ("PUT", contents_url(PENDING)): (201, {"commit": {"sha": "p"}}),
            ("PUT", contents_url(EVIDENCE)): (201, {"commit": {"sha": "e"}}),
        }

    def run_main(self, env=None, telegram=None):
        gh = FakeGitHub(self.routes)
        telegram_calls = []

        def fake_send(token, chat_id, text, urlopen=None):
            telegram_calls.append((token, chat_id, text))
            return (telegram or {"ok": True, "result": {"message_id": 555}})

        with mock.patch.dict("os.environ", env or ENV_OK), \
             mock.patch.object(ra, "send_telegram", side_effect=fake_send), \
             mock.patch.object(ra.urllib.request, "urlopen", gh.urlopen):
            rc = ra.main()
        return rc, gh, telegram_calls

    def test_full_run_sends_once_writes_pending_then_evidence(self):
        rc, gh, telegram_calls = self.run_main()
        self.assertEqual(rc, 0)
        # 送信は 1 通。宛先は bot との 1:1 チャット、本文は単一ソースの固定文言
        self.assertEqual(len(telegram_calls), 1)
        token, chat_id, text = telegram_calls[0]
        self.assertEqual((token, chat_id), ("ttok", "42"))
        from ops.tools.wish_seeds import compose_ask
        self.assertEqual(text, compose_ask())
        # GitHub への PUT は 2 個: 先に pending、後に証跡 (この順が二重送信の歯止め)
        self.assertEqual(len(gh.puts), 2)
        pending_put, evidence_put = gh.puts
        self.assertIn("started_at", json.loads(
            base64.b64decode(pending_put["content"])))
        evidence = json.loads(base64.b64decode(evidence_put["content"]))
        # wrapper が回す verify 2 と同じ判定 + Job 由来であることの欄
        self.assertEqual(evidence["message_id"], 555)
        self.assertTrue(evidence["sent_at"])
        self.assertEqual(evidence["chat_id"], "42")
        self.assertEqual(evidence["via"], "job")

    def test_skip_when_main_has_evidence_never_touches_telegram_or_puts(self):
        self.routes[("GET", contents_url(EVIDENCE))] = (
            200, {"_exists_refs": ["main"]})
        rc, gh, telegram_calls = self.run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(telegram_calls, [])
        self.assertEqual(gh.puts, [])

    def test_missing_env_fails_before_any_request_and_send(self):
        env = dict(ENV_OK, TELEGRAM_BOT_TOKEN="")
        rc, gh, telegram_calls = self.run_main(env=env)
        self.assertEqual(rc, 1)
        self.assertEqual(telegram_calls, [])
        self.assertEqual(gh.calls, [])

    def test_telegram_failure_leaves_pending_guard_behind(self):
        """送信で例外 → Job は落ちる。再実行時は pending があるため送らない。"""
        self.routes[("GET", contents_url(PENDING))] = (200, {"_exists_refs": []})

        def boom(*a, **k):
            raise RuntimeError("telegram down")

        gh = FakeGitHub(dict(self.routes))
        with mock.patch.dict("os.environ", ENV_OK), \
             mock.patch.object(ra, "send_telegram", side_effect=boom), \
             mock.patch.object(ra.urllib.request, "urlopen", gh.urlopen):
            with self.assertRaises(RuntimeError):
                ra.main()
        # pending の PUT は済んでいる (= 歯止めが効いた状態で落ちる)
        self.assertEqual([p["branch"] for p in gh.puts], [BRANCH])

    def test_rerun_after_partial_failure_aborts_without_sending(self):
        """pending があるのに証跡が無い状態での再実行: 送らず abort (rc=1)。"""
        self.routes[("GET", contents_url(PENDING))] = (
            200, {"_exists_refs": [BRANCH]})
        rc, gh, telegram_calls = self.run_main()
        self.assertEqual(rc, 1)
        self.assertEqual(telegram_calls, [])
        self.assertEqual(gh.puts, [])


class TestJobManifest(unittest.TestCase):
    """Job / Application manifest の構造を YAML パースで固定する (ネットワーク無し)。"""

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.job = yaml.safe_load((APP_DIR / "job.yaml").read_text())
        cls.app = yaml.safe_load((APP_DIR / "application.yaml").read_text())

    def pod_spec(self):
        return self.job["spec"]["template"]["spec"]

    def test_job_has_force_replace_annotation(self):
        annotations = self.job["metadata"]["annotations"]
        self.assertEqual(
            annotations.get("argocd.argoproj.io/sync-options"),
            "Force=true,Replace=true",
        )

    def test_job_runs_in_autopilot_namespace(self):
        self.assertEqual(self.job["metadata"]["namespace"], "autopilot")

    def test_job_references_telegram_adapter_credentials(self):
        container = self.pod_spec()["containers"][0]
        env = {
            e["name"]: e.get("valueFrom", {}).get("secretKeyRef", {})
            for e in container["env"]
        }
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID",
                     "AUTOPILOT_GITHUB_TOKEN"):
            self.assertEqual(env[name]["name"], "telegram-adapter-credentials")
            self.assertEqual(env[name]["key"], name)

    def test_job_has_no_ttl_so_selfheal_does_not_rerun_forever(self):
        self.assertNotIn("ttlSecondsAfterFinished", self.job["spec"])

    def test_job_does_not_mount_service_account_token(self):
        self.assertFalse(self.pod_spec().get("automountServiceAccountToken", True))

    def test_job_command_runs_run_ask_from_configmap(self):
        container = self.pod_spec()["containers"][0]
        self.assertIn("/scripts/run_ask.py", container["command"])
        volumes = {v["name"]: v for v in self.pod_spec()["volumes"]}
        self.assertEqual(
            volumes["script"]["configMap"]["name"], "wish-seeds-script")

    def test_application_tracks_this_dir_in_autopilot_ns(self):
        self.assertEqual(self.app["metadata"]["name"], "wish-seeds")
        self.assertEqual(self.app["spec"]["source"]["path"], "apps/wish-seeds")
        self.assertEqual(self.app["spec"]["destination"]["namespace"], "autopilot")


if __name__ == "__main__":
    unittest.main()

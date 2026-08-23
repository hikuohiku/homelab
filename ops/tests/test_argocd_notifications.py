"""ArgoCD Notifications → Discord 配線 (P-0139) の構造を機械で守る。

なぜ要るか: verify の grep (`grep -q 'on-degraded\\|on-sync-failed' values.yaml`) は
文字列の存在しか見ない。trigger→template 参照の切れ目、ExternalSecret が作る Secret 名と
notifier の `$<key>` 参照の不一致、kustomization resources への載せ忘れはどれも
「ファイルはある・単語はあるが配線していない」状態で、ArgoCD は黙って通知しない。
08-10 の backup 見落とし (test_backup_coverage.py, P-0047) と同じ「置いただけでは
動かない」型をここで構造として固定する。

検査するもの:
  1. values.yaml の notifications ブロックに trigger (on-degraded / on-sync-failed) /
     template / notifier (service.webhook.discord) / global subscription の 4 点がある
  2. 各 trigger の when に autopilot ns 除外 (ノイズフィルタ) が入っていること —
     フィルタなしで戻すと heart 関連の一時 Degraded で毎時鳴る
  3. trigger.send が参照する template が実在すること (切れた参照はエラーにならず
     通知が出ないだけなので、黙ったまま気づけない)
  4. notifier の $discord-webhook-url 参照と ExternalSecret の secretKey、
     ExternalSecret target.name (=argocd-notifications-secret) と
     notifications.secret.create: false の整合
  5. ExternalSecret が kustomization.yaml の resources に載っていること
  6. trigger のキー名に trigger. 接頭辞があること — chart は triggers のキーを cm data に
     verbatim で載せ、controller は trigger.<名前> のキーしか読まない。裸の on-degraded 等
     は「trigger '...' is not configured」で黙って発火しない (2026-08-23 合成障害注入中に
     controller ログで実測した失敗形。verify #4 の drill で初めて露見した)
  7. when が operationState を素の . で辿っていないこと — operationState は任意セクション
     (sync 未実行の App や操作受付直後には無い) で、素の . 辿りはその App の評価を毎 round
     「cannot fetch phase from <nil>」エラーにする (2026-08-23 drill 直後の controller ログで
     実測)。公式ドキュメントと同じ ?. (optional chaining) を使う

**既知の死角**: YAML 構造しか見ない。Discord に実際に届いたかは fired.json (合成障害の
証跡) と controller ログが担う。また render 自体は CI (ci.yml の kustomize build
--enable-helm) の担当で、sandbox に helm は無いためこのテストでも見ない。

リポジトリルートから `python3 -m unittest ops.tests.test_argocd_notifications`。
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
ARGOCD_DIR = ROOT / "apps" / "argocd"

REQUIRED_TRIGGERS = ("on-degraded", "on-sync-failed")
TRIGGER_PREFIX = "trigger."
NOISE_FILTER = "autopilot"
UNSAFE_OPERATION_STATE_ACCESS = "app.status.operationState"
WEBHOOK_SECRET_NAME = "argocd-notifications-secret"
WEBHOOK_URL_KEY = "discord-webhook-url"
DISCORD_SERVICE = "discord"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text()) or {}


def collect_notifications(values: dict) -> dict:
    """values から notifications ブロックだけを取り出す (無ければ空 dict)。"""
    return values.get("notifications") or {}


def find_problems(notifications: dict, kustomization_resources: list,
                  external_secret_doc) -> list:
    """純関数。notifications ブロック + kustomization resources + ExternalSecret ドキュメント
    (無ければ None) から、配線の問題リストを返す。"""
    problems = []

    if not notifications:
        return ["values.yaml に notifications ブロックがない"]

    triggers = notifications.get("triggers") or {}
    templates = notifications.get("templates") or {}
    notifiers = notifications.get("notifiers") or {}
    subscriptions = notifications.get("subscriptions") or []
    secret = notifications.get("secret") or {}

    # (1) trigger キーは trigger.<名前> の形でなければならない (裸キーは controller に無視される)
    for key in triggers:
        if not key.startswith(TRIGGER_PREFIX):
            problems.append(
                f"notifications.triggers のキー '{key}' に '{TRIGGER_PREFIX}' 接頭辞が無い。"
                f"chart はキーを cm data に verbatim で載せ、controller は "
                f"{TRIGGER_PREFIX}<名前> のキーしか読まないため、裸キーは "
                f"'trigger {key} is not configured' で黙って発火しない"
            )

    # (2) trigger の存在とノイズフィルタ
    for name in REQUIRED_TRIGGERS:
        trig = triggers.get(TRIGGER_PREFIX + name)
        if not trig:
            problems.append(
                f"trigger {TRIGGER_PREFIX + name} が values.yaml notifications.triggers に無い"
            )
            continue
        conditions = yaml.safe_load(trig)
        for cond in conditions or []:
            when = cond.get("when", "")
            if NOISE_FILTER not in when:
                problems.append(
                    f"trigger {name} の when に destination.namespace != '{NOISE_FILTER}' "
                    f"によるノイズフィルタが無い。器自身の変化で毎時鳴ることになる"
                )
            if UNSAFE_OPERATION_STATE_ACCESS in when:
                problems.append(
                    f"trigger {name} の when が {UNSAFE_OPERATION_STATE_ACCESS} を素の '.' で"
                    f"辿っている。operationState は sync 未実行の App には存在せず、評価が "
                    f"毎 round 'cannot fetch phase from <nil>' エラーになる (通知は出ないが "
                    f"error ログが積み続く)。公式ドキュメントと同じ "
                    f"app.status?.operationState (optional chaining) を使うこと"
                )

    # (3) trigger.send → template 参照の整合
    for name, trig in triggers.items():
        for cond in yaml.safe_load(trig) or []:
            for send in cond.get("send", []):
                key = f"template.{send}"
                if key not in templates:
                    problems.append(
                        f"trigger {name} の send [{send}] が参照する {key} が "
                        f"notifications.templates に無い。切れた参照は通知が出ないだけで "
                        f"エラーにもならない"
                    )

    # (4) template → notifier (webhook サービス名) 参照の整合
    service_key = f"service.webhook.{DISCORD_SERVICE}"
    if service_key not in notifiers:
        problems.append(
            f"notifications.notifiers に {service_key} が無い。"
            f"subscription の recipient '{DISCORD_SERVICE}' が宛先を見失う"
        )
    else:
        opts = yaml.safe_load(notifiers[service_key]) or {}
        url = opts.get("url", "")
        expected_ref = f"${WEBHOOK_URL_KEY}"
        if url != expected_ref:
            problems.append(
                f"{service_key} の url が {expected_ref} ではない ({url})。"
                f"参照キーを変えたら ExternalSecret の secretKey も同時に変えること"
            )
        headers = [h.get("name", "") for h in opts.get("headers", [])]
        if "Content-Type" not in headers:
            problems.append(
                f"{service_key} に Content-Type ヘッダが無い。Discord webhook は "
                f"application/json で POST する必要がある"
            )
        for name, tpl_value in templates.items():
            tpl = yaml.safe_load(tpl_value) or {}
            body = (tpl.get("webhook") or {}).get(DISCORD_SERVICE) or {}
            if not isinstance(body, dict):
                body = {}
            if not body.get("body"):
                problems.append(
                    f"template {name} に discord 用の body が無い "
                    f"(webhook.{DISCORD_SERVICE}.body)"
                )
            if body.get("method") != "POST":
                problems.append(
                    f"template {name} の method が POST でない "
                    f"(default の GET だと Discord は受け付けない)"
                )

    # (5) subscription が全 trigger を購読していること (人間の Discord 1 本のみ)
    subscribed = set()
    for sub in subscriptions:
        if DISCORD_SERVICE in (sub.get("recipients") or []):
            subscribed.update(sub.get("triggers") or [])
    for name in REQUIRED_TRIGGERS:
        if name not in subscribed:
            problems.append(
                f"trigger {name} が subscription (recipients: {DISCORD_SERVICE}) に "
                f"載っていない。trigger/template を定義しただけでは誰にも届かない"
            )

    # (6) chart 由来の空 Secret との二重管理を防ぐ設定
    if secret.get("create") is not False:
        problems.append(
            "notifications.secret.create が false でない。chart が argocd-notifications-secret "
            "を空のまま作ると ExternalSecret と同じ名前を取り合う"
        )

    # (7) ExternalSecret の整合と kustomization への配線
    if external_secret_doc is None:
        problems.append(
            "apps/argocd/discord-webhook-external-secret.yaml が読めない / 存在しない"
        )
    else:
        spec = external_secret_doc.get("spec") or {}
        target = spec.get("target") or {}
        if target.get("name") != WEBHOOK_SECRET_NAME:
            problems.append(
                f"ExternalSecret target.name が {WEBHOOK_SECRET_NAME} でない。"
                f"$<key> 参照はこの Secret 名からしか解決されない"
            )
        data_keys = {
            entry.get("secretKey")
            for entry in spec.get("data") or []
        }
        if WEBHOOK_URL_KEY not in data_keys:
            problems.append(
                f"ExternalSecret が secretKey {WEBHOOK_URL_KEY} を作っていない "
                f"(data: {sorted(k for k in data_keys if k)})"
            )
        remote = next(
            (e.get("remoteRef", {}).get("key") for e in spec.get("data") or []
             if e.get("secretKey") == WEBHOOK_URL_KEY),
            None,
        )
        if remote != "DISCORD_WEBHOOK_URL":
            problems.append(
                f"ExternalSecret の remoteRef.key が DISCORD_WEBHOOK_URL でない ({remote})"
            )
        file_name = "discord-webhook-external-secret.yaml"
        if file_name not in [str(r).lstrip("./") for r in kustomization_resources]:
            problems.append(
                f"{file_name} が kustomization.yaml の resources に無い。"
                f"置いただけでは ArgoCD は同期しない"
            )

    return problems


class TestRealRepo(unittest.TestCase):
    """実リポジトリに対する検査。"""

    def setUp(self):
        self.values = load_yaml(ARGOCD_DIR / "values.yaml")
        self.kustomization = load_yaml(ARGOCD_DIR / "kustomization.yaml")
        es_path = ARGOCD_DIR / "discord-webhook-external-secret.yaml"
        self.es = load_yaml(es_path) if es_path.exists() else None
        self.problems = find_problems(
            collect_notifications(self.values),
            self.kustomization.get("resources") or [],
            self.es,
        )

    def test_wiring_is_complete(self):
        self.assertEqual(self.problems, [], "\n" + "\n".join(self.problems))

    def test_scan_actually_sees_something(self):
        """走査そのものが壊れて空を返すと、上のテストは黙って通ってしまう。"""
        notifications = collect_notifications(self.values)
        for name in REQUIRED_TRIGGERS:
            self.assertIn(TRIGGER_PREFIX + name, notifications.get("triggers") or {})
        self.assertIn("discord-webhook-external-secret.yaml",
                      self.kustomization.get("resources") or [])

    def test_verify_grep_condition_holds(self):
        r"""受入 verify #1 (`grep -q 'on-degraded\|on-sync-failed'`) の再現。"""
        text = (ARGOCD_DIR / "values.yaml").read_text()
        self.assertIn("on-degraded", text)
        self.assertIn("on-sync-failed", text)


class TestFindProblems(unittest.TestCase):
    """判定の両方向を合成入力で固定する (実 repo だけでは「たまたま通っている」を検出できない)。"""

    def make_notifications(self, **overrides):
        base = {
            "argocdUrl": "https://argocd.example",
            "secret": {"create": False},
            "notifiers": {
                "service.webhook.discord": (
                    "url: $discord-webhook-url\nheaders:\n"
                    "  - name: Content-Type\n    value: application/json\n"
                ),
            },
            "templates": {
                "template.discord-app-degraded": (
                    "webhook:\n  discord:\n    method: POST\n"
                    "    body: |\n      {\"content\": \"degraded\"}\n"
                ),
                "template.discord-app-sync-failed": (
                    "webhook:\n  discord:\n    method: POST\n"
                    "    body: |\n      {\"content\": \"sync failed\"}\n"
                ),
            },
            "triggers": {
                "trigger.on-degraded": (
                    "- when: app.status.health.status == 'Degraded'"
                    " && app.spec.destination.namespace != 'autopilot'\n"
                    "  send: [discord-app-degraded]\n"
                ),
                "trigger.on-sync-failed": (
                    "- when: app.status?.operationState.phase in ['Error', 'Failed']"
                    " && app.spec.destination.namespace != 'autopilot'\n"
                    "  send: [discord-app-sync-failed]\n"
                ),
            },
            "subscriptions": [
                {"recipients": ["discord"],
                 "triggers": ["on-degraded", "on-sync-failed"]},
            ],
        }
        base.update(overrides)
        return base

    RESOURCES = ["ingress.yaml", "discord-webhook-external-secret.yaml"]

    ES_DOC = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": "argocd-notifications-discord-webhook", "namespace": "argocd"},
        "spec": {
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": "doppler"},
            "target": {"name": "argocd-notifications-secret", "creationPolicy": "Owner"},
            "data": [{"secretKey": "discord-webhook-url",
                      "remoteRef": {"key": "DISCORD_WEBHOOK_URL"}}],
        },
    }

    def test_complete_wiring_passes(self):
        self.assertEqual(
            find_problems(self.make_notifications(), self.RESOURCES, self.ES_DOC), [],
        )

    def test_broken_send_reference_fails(self):
        notifications = self.make_notifications()
        notifications["triggers"]["trigger.on-degraded"] = (
            "- when: app.status.health.status == 'Degraded'\n"
            "  send: [no-such-template]\n"
        )
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("no-such-template" in p for p in problems))

    def test_missing_noise_filter_fails(self):
        notifications = self.make_notifications()
        notifications["triggers"]["trigger.on-degraded"] = (
            "- when: app.status.health.status == 'Degraded'\n"
            "  send: [discord-app-degraded]\n"
        )
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("ノイズフィルタ" in p for p in problems))

    def test_bare_trigger_key_fails(self):
        """接頭辞なしの trigger キーは controller に黙って無視される (2026-08-23 実測の失敗形)。"""
        notifications = self.make_notifications()
        notifications["triggers"] = {
            k[len(TRIGGER_PREFIX):]: v for k, v in notifications["triggers"].items()
        }
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("接頭辞" in p for p in problems))
        self.assertTrue(any("not configured" in p for p in problems))

    def test_bare_operation_state_access_fails(self):
        """operationState を素の . で辿る when は、sync 未実行 App の評価を毎 round
        「cannot fetch phase from <nil>」エラーにする (2026-08-23 drill 直後の
        controller ログ実測)。?. (optional chaining) を必須にする。"""
        notifications = self.make_notifications()
        notifications["triggers"]["trigger.on-sync-failed"] = (
            "- when: app.status.operationState.phase in ['Error', 'Failed']"
            " && app.spec.destination.namespace != 'autopilot'\n"
            "  send: [discord-app-sync-failed]\n"
        )
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("optional chaining" in p for p in problems))

    def test_unsubscribed_trigger_fails(self):
        notifications = self.make_notifications(subscriptions=[])
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("subscription" in p for p in problems))

    def test_chart_managed_secret_fails(self):
        notifications = self.make_notifications(secret={"create": True})
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("create" in p for p in problems))

    def test_wrong_secret_key_reference_fails(self):
        notifications = self.make_notifications()
        notifications["notifiers"]["service.webhook.discord"] = (
            "url: $typo-webhook-url\n"
        )
        problems = find_problems(notifications, self.RESOURCES, self.ES_DOC)
        self.assertTrue(any("$discord-webhook-url" in p for p in problems))

    def test_external_secret_not_wired_into_kustomization_fails(self):
        problems = find_problems(self.make_notifications(), ["ingress.yaml"], self.ES_DOC)
        self.assertTrue(any("resources" in p for p in problems))

    def test_wrong_target_secret_name_fails(self):
        es = {"spec": {"target": {"name": "some-other-secret"},
                       "data": [{"secretKey": "discord-webhook-url",
                                 "remoteRef": {"key": "DISCORD_WEBHOOK_URL"}}]}}
        problems = find_problems(self.make_notifications(), self.RESOURCES, es)
        self.assertTrue(any("target.name" in p for p in problems))

    def test_missing_external_secret_file_fails(self):
        problems = find_problems(self.make_notifications(), self.RESOURCES, None)
        self.assertTrue(any("discord-webhook-external-secret.yaml" in p for p in problems))


if __name__ == "__main__":
    unittest.main()

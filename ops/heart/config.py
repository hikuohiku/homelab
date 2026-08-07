"""heart の設定読み込み。

単一情報源は ops/rules.json / ops/models.json (PR 経由でのみ変わる)。
環境変数は「どこで動いているか」(パス・トークン・モード) だけを持ち、
「どう振る舞うか」(閾値・モデル) を持たない。両者を混ぜない。
"""

import json
import os
from pathlib import Path


class Config:
    def __init__(self, repo_dir, rules, models, env):
        self.repo_dir = Path(repo_dir)
        self.rules = rules
        self.models = models
        # shadow: spawn / merge / Discord 送信 / ops-state push 以外の副作用を殺し、
        # 判断だけを実データで回す並走検証モード (プラン Phase 1)
        self.mode = env.get("HEART_MODE", "shadow")
        self.namespace = env.get("HEART_NAMESPACE", "autopilot")
        self.repo = env.get("GITHUB_REPO", "hikuohiku/homelab")
        self.github_token = env.get("AUTOPILOT_GITHUB_TOKEN_V2") or env.get(
            "AUTOPILOT_GITHUB_TOKEN", ""
        )
        self.discord_webhook = env.get("DISCORD_WEBHOOK_URL", "")
        self.data_dir = Path(env.get("HEART_DATA_DIR", "/data"))
        self.state_branch = env.get("HEART_STATE_BRANCH", "ops-state")
        self.health_branch = env.get("HEALTH_BRANCH", "ops-health-report")
        self.feedback_branch = env.get("FEEDBACK_BRANCH", "ops-feedback")
        self.feedback_issue = int(env.get("FEEDBACK_ISSUE", "56"))
        self.beat_seconds = int(env.get("HEART_BEAT_SECONDS", "120"))
        self.image = env.get("AUTOPILOT_IMAGE", "")

    @property
    def shadow(self):
        return self.mode != "active"

    def model_for(self, role):
        return self.models["roles"][role]


def load(repo_dir, env=None):
    env = dict(os.environ) if env is None else env
    repo = Path(repo_dir)
    with open(repo / "ops" / "rules.json") as f:
        rules = json.load(f)
    with open(repo / "ops" / "models.json") as f:
        models = json.load(f)
    return Config(repo, rules, models, env)

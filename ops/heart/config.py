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
        # shadow: spawn / merge / Discord 送信以外の副作用を殺し、
        # 判断だけを実データで回す並走検証モード (プラン Phase 1)
        self.mode = env.get("HEART_MODE", "shadow")
        self.namespace = env.get("HEART_NAMESPACE", "autopilot")
        self.repo = env.get("GITHUB_REPO", "hikuohiku/homelab")
        self.github_token = env.get("AUTOPILOT_GITHUB_TOKEN_V2") or env.get(
            "AUTOPILOT_GITHUB_TOKEN", ""
        )
        self.discord_webhook = env.get("DISCORD_WEBHOOK_URL", "")
        self.data_dir = Path(env.get("HEART_DATA_DIR", "/data"))
        # 健全性レポートの置き場所 (設計 state-out-of-git Phase 5)。ops-health-reporter が
        # 30 分ごとに同じ namespace の ConfigMap を上書きする。GitHub を経由しない
        self.health_configmap = env.get("HEALTH_CONFIGMAP", "ops-health-report")
        self.feedback_issue = int(env.get("FEEDBACK_ISSUE", "56"))
        # 書き置きが落ちてくる場所 (同じ Pod のサイドカーが書く。
        # apps/autopilot/bus-sidecar)。既定値はサイドカーの BUS_SIDECAR_OUT_DIR と
        # 揃えてあり、どちらも通常は設定しなくてよい。ops-feedback ブランチを畳んだ
        # 今 (Phase 6)、ファイルで書き置きが届く経路はここ 1 本だけ
        self.feedback_bus_dir = Path(
            env.get("HEART_FEEDBACK_BUS_DIR") or (self.data_dir / "feedback-bus" / "inbox")
        )
        # 常駐コア発の command (設計 D3/D21) が落ちてくる場所。書き置きと**別**の
        # ディレクトリなのは、混ぜると triage が人間の発話として誤分類するため。
        # 既定値はサイドカーの BUS_SIDECAR_COMMAND_DIR と揃えてある
        self.command_bus_dir = Path(
            env.get("HEART_COMMAND_BUS_DIR") or (self.data_dir / "command-bus" / "inbox")
        )
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

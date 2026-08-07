"""Kubernetes Job の組み立てと起動。

apps/coder/workspace-home-backup-cronjob.yaml のオーケストレータと同じ流儀:
Job spec を dict で組み立てて API に POST、409 (前回分が GC 待ち) は正常扱い。
削除権限は veto の即 kill に使う。GC は ttlSecondsAfterFinished に任せる。

SA の選択がここの安全上の本体 (決定 #5 宣言制注入):
  - 既定は autopilot-runner (クラスタへの書き込み権限なし、トークン automount なし)
  - PROJECT.md が capabilities に "kubectl-write" を宣言し、予告にもそれが
    載ったプロジェクトの Job にだけ autopilot-writer を注入する
"""

from .k8s import K8sError


def _job_name(kind, project_id, attempt):
    base = f"{kind}-{project_id.lower()}-a{attempt}"
    return base[:63].rstrip("-")


def build_job(cfg, kind, *, project=None, attempt=0, extra_env=None):
    """kind: runner | reviewer | curriculum | consolidation | critic | chore"""
    pid = project["id"] if project else "system"
    name = _job_name(kind, pid, attempt)
    capabilities = (project or {}).get("capabilities", [])
    use_writer = kind == "runner" and "kubectl-write" in capabilities
    sa = "autopilot-writer" if use_writer else "autopilot-runner"
    # worker はクラスタ API に触れない (automount 無し)。reviewer は read プローブの
    # ために読み取りトークンを持つ (autopilot-runner は autopilot-reader に bind 済み)
    automount = use_writer or kind == "reviewer"

    role_env = {
        "runner": "worker",
        "reviewer": "review",
        "curriculum": "curriculum",
        "consolidation": "consolidation",
        "critic": "critic",
        "chore": "chore",
    }[kind]
    model_role = {
        "runner": "project",
        "reviewer": "review",
        "curriculum": "curriculum_generate",
        "consolidation": "consolidation",
        "critic": "critic",
        "chore": "chore",
    }[kind]

    env = [
        {"name": "RUNNER_MODE", "value": role_env},
        {"name": "PROJECT_ID", "value": pid},
        {"name": "CLAUDE_MODEL", "value": cfg.model_for(model_role)},
        {"name": "GITHUB_REPO", "value": cfg.repo},
        {"name": "HOME", "value": "/work/home"},
        {"name": "NPM_CONFIG_CACHE", "value": "/work/.npm"},
        {"name": "GIT_AUTHOR_NAME", "value": "homelab-autopilot"},
        {"name": "GIT_AUTHOR_EMAIL", "value": "autopilot@hikuohiku.dev"},
        {"name": "GIT_COMMITTER_NAME", "value": "homelab-autopilot"},
        {"name": "GIT_COMMITTER_EMAIL", "value": "autopilot@hikuohiku.dev"},
        {
            "name": "CLAUDE_CODE_OAUTH_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "autopilot-credentials",
                    "key": "CLAUDE_CODE_OAUTH_TOKEN",
                }
            },
        },
        {
            "name": "AUTOPILOT_GITHUB_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "autopilot-credentials",
                    "key": "AUTOPILOT_GITHUB_TOKEN",
                }
            },
        },
    ]
    if project:
        env.append({"name": "PROJECT_BRANCH", "value": project.get("branch", "")})
    for k, v in (extra_env or {}).items():
        env.append({"name": k, "value": str(v)})

    # 実行コードは main の repo から取る (イメージに焼き込まない — loop.sh と同方針)。
    # bootstrap はここに閉じる: clone して ops/runner/runner.py に exec するだけ
    # mkdir が git config --global より先であること: git config は $HOME/.gitconfig を
    # 書くが親ディレクトリを作らないため、順序を誤ると全 Job が起動直後に必ず落ちる
    # (レビュー指摘 [15]。heart-bootstrap.sh と同順)
    bootstrap = (
        "set -eu; "
        "mkdir -p \"$HOME\" /work; "
        "git config --global credential.\"https://github.com\".helper "
        "'!f() { printf \"username=x-access-token\\npassword=%s\\n\" "
        "\"${AUTOPILOT_GITHUB_TOKEN}\"; }; f'; "
        "git clone --quiet https://github.com/" + cfg.repo + ".git /work/repo; "
        "cd /work/repo; exec python3 ops/runner/runner.py"
    )

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "heart",
                "heart/kind": kind,
                "heart/project": pid.lower(),
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 259200,
            "ttlSecondsAfterFinished": 21600,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/managed-by": "heart",
                        "heart/kind": kind,
                        "heart/project": pid.lower(),
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": sa,
                    "automountServiceAccountToken": automount,
                    "securityContext": {
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                    },
                    "containers": [
                        {
                            "name": kind,
                            "image": cfg.image,
                            "command": ["bash", "-c", bootstrap],
                            "env": env,
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "work", "mountPath": "/work"},
                            ],
                            "resources": {
                                "requests": {"cpu": "200m", "memory": "512Mi"}
                                # memory limits は実測の裏付けなしに付けない (CHARTER §4)
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "autopilot-data"},
                        },
                        {"name": "work", "emptyDir": {"sizeLimit": "4Gi"}},
                    ],
                },
            },
        },
    }


def create(k8s, cfg, kind, *, project=None, attempt=0, extra_env=None):
    """Job を作成する。既に同名があれば (409) その名前を返して正常扱い。"""
    job = build_job(cfg, kind, project=project, attempt=attempt, extra_env=extra_env)
    name = job["metadata"]["name"]
    try:
        k8s.create_job(cfg.namespace, job)
    except K8sError as e:
        if e.status != 409:
            raise
    return name

import { getKubeSnapshot } from "./kubernetes";
import { getOpsState } from "./ops-state";
import { latestAction } from "./transcript";
import type { AgentSnapshot, AttentionItem, Project, Snapshot } from "./types";

// budget_exhausted は 2026-08-24 に session_limit へ改名。過去の projects.json も読むので両方残す
const QUESTION_REASONS = new Set(["session_limit", "budget_exhausted", "quota_wait_exhausted", "merge_timeout", "pr_closed"]);
// 読み先が Project CR に移っても rejected は入らない (ops-state.ts が selector で
// 外し、コード側でも落としている)。棄却案 250 件超はボードの流れではなく立案役の
// 教師信号で、混ぜると終端の山に他が埋まる — 意図的に FLOW_ORDER に無い
const FLOW_ORDER = ["proposed", "announced", "active", "in_review", "merging", "soaking", "delivered", "stalled", "vetoed"];

export function buildAttention(projects: Project[], now = new Date()): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const project of projects) {
    if (project.acknowledged) continue; // ack P-NNNN 済みの墓標は要対応に出さない
    if (project.state === "stalled") {
      const reason = project.stalled_reason || "理由未記録";
      const question = QUESTION_REASONS.has(reason) || reason.startsWith("adopt_gate_");
      items.push({
        id: project.id,
        kind: question ? "question" : "stalled",
        title: project.title,
        detail: question ? `回答待ち: ${reason}` : `停止: ${reason}`,
        irreversible: project.irreversible,
      });
    }
    if (project.state === "announced" && project.veto_deadline) {
      const deadline = new Date(project.veto_deadline);
      if (deadline.getTime() > now.getTime()) {
        items.push({
          id: project.id,
          kind: "veto",
          title: project.title,
          detail: `拒否する場合は issue #56 に「veto ${project.id}」`,
          deadline: project.veto_deadline,
          irreversible: project.irreversible,
        });
      }
    }
  }
  return items.sort((a, b) => {
    const priority = { question: 0, veto: 1, stalled: 2 };
    return priority[a.kind] - priority[b.kind] || (a.deadline ?? "z").localeCompare(b.deadline ?? "z");
  });
}

export async function getSnapshot(): Promise<Snapshot> {
  const [state, kube] = await Promise.all([getOpsState(), getKubeSnapshot()]);
  const projectsById = new Map(state.projects.map((project) => [project.id.toUpperCase(), project]));
  const jobAgents = await Promise.all(kube.jobs.map(async (job) => {
    const action = await latestAction(job.role, job.projectId);
    return {
      ...job,
      projectTitle: projectsById.get(job.projectId)?.title,
      recentAction: action.text,
      transcriptAvailable: action.available,
    };
  }));
  // 常駐組 (heart/resident Deployment) は projectId / transcript を持たない。
  // 「応答可能か」= Ready 数を recentAction の位置に出す
  const residentAgents: AgentSnapshot[] = kube.residents.map((r) => ({
    id: r.id,
    role: r.role,
    projectId: r.id.toUpperCase(),
    projectTitle: "常駐エージェント",
    startedAt: r.startedAt,
    podPhase: r.podPhase,
    recentAction: `Ready ${r.readyReplicas}/${r.replicas}`,
    transcriptAvailable: false,
    resident: true,
  }));
  const agents = [...jobAgents, ...residentAgents];
  const heartbeatAt = state.heartbeat.at ? Date.parse(state.heartbeat.at) : 0;
  const usage = state.heartbeat.usage ?? {};
  const warnings = [state.warning, kube.warning].filter((value): value is string => Boolean(value));
  const projects = [...state.projects].sort((a, b) => {
    const ai = FLOW_ORDER.indexOf(a.state);
    const bi = FLOW_ORDER.indexOf(b.state);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || a.id.localeCompare(b.id);
  });
  return {
    generatedAt: new Date().toISOString(),
    agents,
    projects,
    attention: buildAttention(projects),
    heart: {
      beat: state.heartbeat.beat,
      at: state.heartbeat.at,
      stale: !heartbeatAt || Date.now() - heartbeatAt > 5 * 60 * 1000,
      deploymentReady: kube.heartReady,
      stopEngaged: state.stopEngaged,
    },
    todayCostUsd: Number(usage.cost_usd ?? 0),
    todayTokens: Number(usage.tokens ?? 0),
    todaySessions: Number(usage.sessions ?? 0),
    todayEmptySessions: Number(usage.empty_sessions ?? 0),
    warnings,
  };
}


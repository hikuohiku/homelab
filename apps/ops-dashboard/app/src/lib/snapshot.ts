import { getKubeSnapshot } from "./kubernetes";
import { getOpsState } from "./ops-state";
import { latestAction } from "./transcript";
import type { AttentionItem, Project, Snapshot } from "./types";

const QUESTION_REASONS = new Set(["budget_exhausted", "quota_wait_exhausted", "merge_timeout", "pr_closed"]);
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
  const agents = await Promise.all(kube.jobs.map(async (job) => {
    const action = await latestAction(job.role, job.projectId);
    return {
      ...job,
      projectTitle: projectsById.get(job.projectId)?.title,
      recentAction: action.text,
      transcriptAvailable: action.available,
    };
  }));
  const heartbeatAt = state.heartbeat.at ? Date.parse(state.heartbeat.at) : 0;
  const breaker = state.metrics.breaker ?? {};
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
    todayCostUsd: Number(breaker.cost_usd ?? 0),
    todaySessions: Number(breaker.sessions ?? 0),
    warnings,
    reminders: state.remindersText || undefined,
  };
}


export type AgentRole =
  | "worker"
  | "reviewer"
  | "curriculum"
  | "critic"
  | "consolidation"
  | "chore"
  | "unknown";

export type TranscriptKind =
  | "thinking"
  | "message"
  | "tool"
  | "result"
  | "error"
  | "system";

export interface TokenUsage {
  input: number;
  output: number;
  total: number;
  costUsd: number;
}

export interface TranscriptEvent {
  id: string;
  kind: TranscriptKind;
  at?: string;
  text?: string;
  toolName?: string;
  input?: unknown;
  output?: unknown;
  status?: "running" | "completed" | "failed";
  usage?: TokenUsage;
}

export interface AgentSnapshot {
  id: string;
  role: AgentRole;
  projectId: string;
  projectTitle?: string;
  startedAt: string;
  podPhase: string;
  recentAction: string;
  transcriptAvailable: boolean;
}

export interface Project {
  id: string;
  title: string;
  state: string;
  branch?: string;
  veto_deadline?: string;
  irreversible?: boolean;
  stalled_reason?: string;
  acknowledged?: boolean;
  review_cycles?: number;
  merging_since?: string;
  prs?: number[];
  budget?: { used_tokens?: number; soft_cap?: number };
}

export interface AttentionItem {
  id: string;
  kind: "stalled" | "veto" | "question";
  title: string;
  detail: string;
  deadline?: string;
  irreversible?: boolean;
}

// seeds.md の『人間の鍵作業』から機械抽出した、人間への依頼 (P-0272)。
// 器からは進められない物理・認証系の仕事で、人間が見ない限り滞留し続ける
export interface HumanTask {
  id: string;
  title: string;
  ageDays: number;
  created?: string;
}

export interface HeartStatus {
  beat?: number;
  at?: string;
  stale: boolean;
  deploymentReady: boolean;
  stopEngaged: boolean;
}

export interface Snapshot {
  generatedAt: string;
  agents: AgentSnapshot[];
  projects: Project[];
  attention: AttentionItem[];
  humanTasks: HumanTask[];
  heart: HeartStatus;
  todayCostUsd: number;
  todaySessions: number;
  warnings: string[];
}

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
  heart: HeartStatus;
  todayCostUsd: number;
  todaySessions: number;
  warnings: string[];
  // P-0231: 暦の描画済み断片。未取得 (heart 未公開) は undefined
  reminders?: string;
}

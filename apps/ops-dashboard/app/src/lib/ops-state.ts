import { execFile } from "node:child_process";
import { mkdir, readFile } from "node:fs/promises";
import { promisify } from "node:util";
import type { Project } from "./types";

const exec = promisify(execFile);
const REPOSITORY = process.env.HOMELAB_REPOSITORY ?? "https://github.com/hikuohiku/homelab.git";
const CACHE_DIR = process.env.OPS_STATE_CACHE_DIR ?? "/tmp/mission-control-state";
const LOCAL_DIR = process.env.OPS_STATE_DIR;
const REFRESH_MS = Number(process.env.OPS_STATE_REFRESH_MS ?? 20_000);

interface OpsState {
  projects: Project[];
  heartbeat: { beat?: number; at?: string };
  metrics: { breaker?: { cost_usd?: number; sessions?: number } };
  // P-0231: 暦の描画済み断片 (briefing/reminders.txt)。heart が各ビートで
  // 作る。無い間は空文字 (「次の予定」節は消さない)
  remindersText: string;
  stopEngaged: boolean;
  warning?: string;
}

let cached: { loadedAt: number; value: OpsState } | undefined;
let refreshInFlight: Promise<OpsState> | undefined;

function parseJson<T>(text: string, fallback: T): T {
  try { return JSON.parse(text) as T; } catch { return fallback; }
}

function parseJsonlLast(text: string): Record<string, unknown> {
  const lines = text.trim().split("\n").reverse();
  for (const line of lines) {
    try { return JSON.parse(line) as Record<string, unknown>; } catch { /* skip broken tail */ }
  }
  return {};
}

function mergeArchive(projects: Project[], archiveText: string): Project[] {
  const specs = new Map<string, Record<string, unknown>>();
  for (const line of archiveText.split("\n")) {
    try {
      const value = JSON.parse(line) as Record<string, unknown>;
      if (typeof value.id === "string") specs.set(value.id, value);
    } catch { /* append-only logs may have an incomplete final line */ }
  }
  return projects.map((project) => {
    const spec = specs.get(project.id);
    return {
      ...project,
      title: project.title || String(spec?.title ?? project.id),
      irreversible: project.irreversible ?? Boolean(spec?.irreversible),
    };
  });
}

async function git(args: string[]): Promise<string> {
  const result = await exec("git", args, {
    cwd: CACHE_DIR,
    timeout: 30_000,
    maxBuffer: 16 * 1024 * 1024,
    env: { ...process.env, GIT_TERMINAL_PROMPT: "0" },
  });
  return result.stdout;
}

async function ensureRepository(): Promise<void> {
  await mkdir(CACHE_DIR, { recursive: true });
  try {
    await git(["rev-parse", "--git-dir"]);
  } catch {
    await git(["init"]);
    await git(["remote", "add", "origin", REPOSITORY]);
  }
}

async function loadFromGit(): Promise<OpsState> {
  await ensureRepository();
  await git([
    "fetch", "--quiet", "--no-tags", "--depth=1", "origin",
    "+refs/heads/ops-state:refs/remotes/origin/ops-state",
    "+refs/heads/main:refs/remotes/origin/main",
  ]);
  // briefing/reminders.txt は P-0231 以降にしか存在しない。git show が
  // 失敗したら空文字に落とす (欠損で取得全体を失敗させない)
  const [projectsText, heartbeatText, metricsText, archiveText, remindersText] = await Promise.all([
    git(["show", "origin/ops-state:projects.json"]),
    git(["show", "origin/ops-state:heartbeat.json"]),
    git(["show", "origin/ops-state:metrics.jsonl"]),
    git(["show", "origin/main:ops/projects/archive.jsonl"]),
    git(["show", "origin/ops-state:briefing/reminders.txt"]).catch(() => ""),
  ]);
  const projectDoc = parseJson<{ projects?: Project[]; stop_engaged?: boolean }>(projectsText, {});
  return {
    projects: mergeArchive(projectDoc.projects ?? [], archiveText),
    heartbeat: parseJson(heartbeatText, {}),
    metrics: parseJsonlLast(metricsText),
    remindersText,
    stopEngaged: Boolean(projectDoc.stop_engaged),
  };
}

async function loadFromDirectory(directory: string): Promise<OpsState> {
  const [projectsText, heartbeatText, metricsText, archiveText, remindersText] = await Promise.all([
    readFile(`${directory}/projects.json`, "utf8"),
    readFile(`${directory}/heartbeat.json`, "utf8"),
    readFile(`${directory}/metrics.jsonl`, "utf8"),
    readFile(`${directory}/archive.jsonl`, "utf8").catch(() => ""),
    readFile(`${directory}/briefing/reminders.txt`, "utf8").catch(() => ""),
  ]);
  const projectDoc = parseJson<{ projects?: Project[]; stop_engaged?: boolean }>(projectsText, {});
  return {
    projects: mergeArchive(projectDoc.projects ?? [], archiveText),
    heartbeat: parseJson(heartbeatText, {}),
    metrics: parseJsonlLast(metricsText),
    remindersText,
    stopEngaged: Boolean(projectDoc.stop_engaged),
  };
}

async function refresh(): Promise<OpsState> {
  try {
    const value = LOCAL_DIR ? await loadFromDirectory(LOCAL_DIR) : await loadFromGit();
    cached = { loadedAt: Date.now(), value };
    return value;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (cached) return { ...cached.value, warning: `ops-state 更新失敗: ${message}` };
    return { projects: [], heartbeat: {}, metrics: {}, remindersText: "", stopEngaged: false, warning: `ops-state 取得失敗: ${message}` };
  } finally {
    refreshInFlight = undefined;
  }
}

export async function getOpsState(): Promise<OpsState> {
  if (cached && Date.now() - cached.loadedAt < REFRESH_MS) return cached.value;
  refreshInFlight ??= refresh();
  return refreshInFlight;
}


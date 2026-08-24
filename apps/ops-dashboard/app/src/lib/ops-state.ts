import { execFile } from "node:child_process";
import { mkdir, readFile } from "node:fs/promises";
import { promisify } from "node:util";
import { kubeGet } from "./kubernetes";
import type { Project } from "./types";

const exec = promisify(execFile);
const REPOSITORY = process.env.HOMELAB_REPOSITORY ?? "https://github.com/hikuohiku/homelab.git";
const CACHE_DIR = process.env.OPS_STATE_CACHE_DIR ?? "/tmp/mission-control-state";
const LOCAL_DIR = process.env.OPS_STATE_DIR;
const REFRESH_MS = Number(process.env.OPS_STATE_REFRESH_MS ?? 20_000);
const NAMESPACE = process.env.AUTOPILOT_NAMESPACE ?? "autopilot";

// Project CR (設計 state-out-of-git 4b-2a)。プロジェクトの読み先はここに移った。
// **棄却案 (state: rejected) はサーバ側で外す** — 250 件を超える終端の山で、
// ボードに出す意味が無いうえ全件を引けばレスポンスが桁で重くなる。
// 設計の「live set は問い合わせ側の selector で切る」がここに効いている。
const PROJECTS_PATH =
  `/apis/autopilot.homelab.hikuohiku.dev/v1/namespaces/${NAMESPACE}/projects` +
  `?labelSelector=${encodeURIComponent("state!=rejected")}&limit=500`;

interface DailyUsage {
  cost_usd?: number; tokens?: number; sessions?: number; empty_sessions?: number;
}

// 心拍は **まだ ops-state ブランチ**、「止めて」は HeartState CR (4b-2b)
interface HeartState {
  heartbeat: { beat?: number; at?: string; usage?: DailyUsage };
  stopEngaged: boolean;
}

interface OpsState extends HeartState {
  projects: Project[];
  warning?: string;
}

let cached: { loadedAt: number; value: OpsState } | undefined;
let refreshInFlight: Promise<OpsState> | undefined;

function parseJson<T>(text: string, fallback: T): T {
  try { return JSON.parse(text) as T; } catch { return fallback; }
}

// CR の spec が projects.json の 1 エントリそのもの。その中の spec 子は
// **立案時の spec** で別物なので、題名などの穴埋め以外には使わない
interface ProjectCr {
  spec?: Project & { spec?: { title?: string; irreversible?: boolean } };
}

export function projectsFromCrs(doc: { items?: ProjectCr[] }): Project[] {
  const projects: Project[] = [];
  for (const item of doc.items ?? []) {
    if (!item.spec || typeof item.spec.id !== "string" || !item.spec.id) continue;
    if (item.spec.state === "rejected") continue; // selector を通さない経路でも混ぜない
    // proposal (= 入れ子の spec) は表示の穴埋めにだけ使い、ボードには載せない
    const { spec: proposal, ...project } = item.spec;
    projects.push({
      ...project,
      title: project.title || proposal?.title || project.id,
      irreversible: project.irreversible ?? Boolean(proposal?.irreversible),
    });
  }
  return projects.sort((a, b) => a.id.localeCompare(b.id));
}

async function loadProjects(): Promise<Project[]> {
  if (LOCAL_DIR) {
    const text = await readFile(`${LOCAL_DIR}/projects.json`, "utf8");
    return parseJson<{ projects?: Project[] }>(text, {}).projects ?? [];
  }
  return projectsFromCrs(await kubeGet(PROJECTS_PATH));
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

// 「止めて」が効いているかは HeartState CR から読む (設計 4b-2b)。
// projects.json への書き込みが止まったので、git 側の stop_engaged は凍った古い値。
// heart の /healthz を見ないのは、**heart が死んだと見せる**のがこの画面の
// 存在意義の一つで、heart 経由では止まった瞬間に読めなくなるため
const HEART_STATE_PATH =
  `/apis/autopilot.homelab.hikuohiku.dev/v1/namespaces/${NAMESPACE}/heartstates/heart`;

export function stopEngagedFromCr(cr: { spec?: { stop_engaged?: boolean } }): boolean {
  return Boolean(cr?.spec?.stop_engaged);
}

async function loadStopEngaged(): Promise<boolean> {
  if (LOCAL_DIR) {
    const text = await readFile(`${LOCAL_DIR}/heart-state.json`, "utf8").catch(() => "{}");
    return Boolean(parseJson<{ stop_engaged?: boolean }>(text, {}).stop_engaged);
  }
  return stopEngagedFromCr(await kubeGet(HEART_STATE_PATH));
}

// 心拍は **まだ ops-state ブランチ**。Lease 化は設計の Phase 7 で、ここでは触らない
async function loadHeartbeat(): Promise<HeartState["heartbeat"]> {
  if (LOCAL_DIR) {
    return parseJson(await readFile(`${LOCAL_DIR}/heartbeat.json`, "utf8"), {});
  }
  await ensureRepository();
  await git([
    "fetch", "--quiet", "--no-tags", "--depth=1", "origin",
    "+refs/heads/ops-state:refs/remotes/origin/ops-state",
  ]);
  return parseJson(await git(["show", "origin/ops-state:heartbeat.json"]), {});
}

// 読み先が 3 つ (Project CR / HeartState CR / ops-state の心拍) になったので、
// 1 つの失敗で残りまで
// 巻き添えにしない。**黙って空を返さない** — プロジェクト 0 件は「全部終わった」に
// 見えるので、直近の写しがあれば警告つきで出し、無ければ取得失敗だと言い切る
async function refresh(): Promise<OpsState> {
  try {
    return await refreshOnce();
  } finally {
    refreshInFlight = undefined;
  }
}

async function refreshOnce(): Promise<OpsState> {
  const previous = cached?.value;
  const warnings: string[] = [];
  let projects = previous?.projects ?? [];
  try {
    projects = await loadProjects();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    warnings.push(
      `プロジェクト (Project CR) 取得失敗: ${message}` +
      (previous ? "（直近の写しを表示中）" : "（表示できるものが無い）"),
    );
  }
  let heartbeat = previous?.heartbeat ?? {};
  try {
    heartbeat = await loadHeartbeat();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    warnings.push(`心拍 (ops-state) 更新失敗: ${message}`);
  }
  // **読めないときは直近の値を保つ**。false に倒すと「止めて」と言った後に
  // 画面上だけ動いているように見える
  let stopEngaged = previous?.stopEngaged ?? false;
  try {
    stopEngaged = await loadStopEngaged();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    warnings.push(`停止状態 (HeartState CR) 取得失敗: ${message}`);
  }
  const value: OpsState = {
    projects,
    heartbeat,
    stopEngaged,
    warning: warnings.length ? warnings.join(" / ") : undefined,
  };
  // 警告つきでも cached は更新する (取れた側は新しいので)
  cached = { loadedAt: Date.now(), value };
  return value;
}

export async function getOpsState(): Promise<OpsState> {
  if (cached && Date.now() - cached.loadedAt < REFRESH_MS) return cached.value;
  refreshInFlight ??= refresh();
  return refreshInFlight;
}

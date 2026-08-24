import { readFile } from "node:fs/promises";
import { kubeGet } from "./kubernetes";
import type { Project } from "./types";

const LOCAL_DIR = process.env.OPS_STATE_DIR;
const REFRESH_MS = Number(process.env.OPS_STATE_REFRESH_MS ?? 20_000);
const NAMESPACE = process.env.AUTOPILOT_NAMESPACE ?? "autopilot";
// heart の admission gate。**読むのは GET /healthz だけ** (POST /dispatch は
// ここから打たない)。到達は同一 namespace の NetworkPolicy が許した経路のみ
const HEART_HEALTHZ =
  process.env.HEART_HEALTHZ_URL ?? `http://autopilot-heart.${NAMESPACE}.svc:8099/healthz`;

// Project CR (設計 state-out-of-git 4b-2a)。プロジェクトの読み先はここに移った。
// **棄却案 (state: rejected) はサーバ側で外す** — 250 件を超える終端の山で、
// ボードに出す意味が無いうえ全件を引けばレスポンスが桁で重くなる。
// 設計の「live set は問い合わせ側の selector で切る」がここに効いている。
const PROJECTS_PATH =
  `/apis/autopilot.homelab.hikuohiku.dev/v1/namespaces/${NAMESPACE}/projects` +
  `?labelSelector=${encodeURIComponent("state!=rejected")}&limit=500`;

// heart の生存 (設計 state-out-of-git Phase 7)。**ビートが最後まで通ったときだけ**
// renewTime が進むので、これは「プロセスが生きているか」ではなく「ビートが
// 回っているか」を表す。beat 番号は注記 (判定には使わない)
const LEASE_PATH =
  `/apis/coordination.k8s.io/v1/namespaces/${NAMESPACE}/leases/autopilot-heart`;
const BEAT_ANNOTATION = "autopilot.homelab.hikuohiku.dev/beat";

interface DailyUsage {
  cost_usd?: number; tokens?: number; sessions?: number; empty_sessions?: number;
}

// 4b-2b で読み先が git から離れた: 心拍は Lease、「止めて」と使用量は
// heart の /healthz。ダッシュボードは git を一切触らない
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

/** Lease から心拍を読む (純関数)。renewTime が「最後にビートが通った時刻」。 */
export function heartbeatFromLease(lease: {
  metadata?: { annotations?: Record<string, string> };
  spec?: { renewTime?: string };
}): { beat?: number; at?: string } {
  const beat = Number(lease.metadata?.annotations?.[BEAT_ANNOTATION]);
  return {
    beat: Number.isFinite(beat) && beat > 0 ? beat : undefined,
    at: lease.spec?.renewTime,
  };
}

async function loadProjects(): Promise<Project[]> {
  if (LOCAL_DIR) {
    const text = await readFile(`${LOCAL_DIR}/projects.json`, "utf8");
    return parseJson<{ projects?: Project[] }>(text, {}).projects ?? [];
  }
  return projectsFromCrs(await kubeGet(PROJECTS_PATH));
}

/** heart の /healthz。doc 全体にかかる値 (Project CR には載らない) の唯一の口。 */
async function loadGateHealth(): Promise<{ stopEngaged: boolean; usage: DailyUsage }> {
  const response = await fetch(HEART_HEALTHZ, {
    cache: "no-store",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`heart /healthz ${response.status}`);
  const doc = (await response.json()) as { stop_engaged?: boolean; usage?: DailyUsage };
  return { stopEngaged: Boolean(doc.stop_engaged), usage: doc.usage ?? {} };
}

async function loadHeart(): Promise<HeartState> {
  if (LOCAL_DIR) {
    const [projectsText, heartbeatText] = await Promise.all([
      readFile(`${LOCAL_DIR}/projects.json`, "utf8"),
      readFile(`${LOCAL_DIR}/heartbeat.json`, "utf8"),
    ]);
    return {
      heartbeat: parseJson(heartbeatText, {}),
      stopEngaged: Boolean(parseJson<{ stop_engaged?: boolean }>(projectsText, {}).stop_engaged),
    };
  }
  const [lease, health] = await Promise.all([kubeGet(LEASE_PATH), loadGateHealth()]);
  return {
    heartbeat: { ...heartbeatFromLease(lease), usage: health.usage },
    stopEngaged: health.stopEngaged,
  };
}

// 読み先が 2 つ (CR と heart) になったので、片方の失敗でもう片方まで
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
  let heart: HeartState = { heartbeat: previous?.heartbeat ?? {}, stopEngaged: previous?.stopEngaged ?? false };
  try {
    heart = await loadHeart();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    warnings.push(`heart の状態 (Lease / healthz) 取得失敗: ${message}`);
  }
  const value: OpsState = {
    projects,
    ...heart,
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

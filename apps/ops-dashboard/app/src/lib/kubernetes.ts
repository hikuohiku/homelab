import https from "node:https";
import http from "node:http";
import { readFile } from "node:fs/promises";
import type { AgentRole } from "./types";
import { roleFromKind } from "./transcript";

const NAMESPACE = process.env.AUTOPILOT_NAMESPACE ?? "autopilot";
const SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount";

export interface RunningJob {
  id: string;
  role: AgentRole;
  projectId: string;
  startedAt: string;
  podPhase: string;
}

// heart/resident: "true" label を持つ Deployment = 常駐エージェント (住人)。
// 短命 Job と違い transcript を持たず、Ready 状態で「応答可能か」を表す
export interface Resident {
  id: string;
  role: AgentRole;
  replicas: number;
  readyReplicas: number;
  podPhase: string;
  startedAt: string;
}

export interface KubeSnapshot {
  jobs: RunningJob[];
  residents: Resident[];
  heartReady: boolean;
  warning?: string;
}

type JsonObject = Record<string, any>;

// reporter (apps/ops-health-reporter/report.py) が書く latest.json の
// node_saturation キーから、CPU 飽和前兆 (P-9037) の warnings 文面を作る (純関数)。
// status=warn のときだけ文字列を返し、ok・キー無し・観測失敗 (error) は undefined。
// 実測値 (requests_m/allocatable_m・load_1m/vcpus) を載せて数字で見せる。
export function saturationWarning(reportDoc: JsonObject | undefined): string | undefined {
  const sat = reportDoc?.node_saturation;
  if (!sat || typeof sat !== "object" || sat.status !== "warn") return undefined;
  const reasons = Array.isArray(sat.reasons) ? sat.reasons : [];
  const parts: string[] = [];
  if (reasons.includes("requests_ratio")) {
    parts.push(`CPU requests ${sat.requests_m ?? "?"}m/${sat.allocatable_m ?? "?"}m`);
  }
  if (reasons.includes("load")) {
    parts.push(`load ${sat.load_1m ?? "?"} > vCPU ${sat.vcpus ?? "?"}`);
  }
  const detail = parts.length ? `: ${parts.join(" / ")}` : "";
  return `CPU 飽和前兆 (${sat.node ?? "node01"})${detail}`;
}

// latest.json (autopilot/ops-health-report ConfigMap の data キー) を doc に解釈。
// 読めない・壊れていれば undefined (飽和前兆の表示は best-effort — 巻き添えで
// クラスタ観測全体を止めない)
export function parseReportDoc(configmap: JsonObject | null): JsonObject | undefined {
  if (!configmap || typeof configmap.data !== "object" || configmap.data === null) {
    return undefined;
  }
  const raw = configmap.data["latest.json"];
  if (typeof raw !== "string") return undefined;
  try {
    const doc = JSON.parse(raw);
    return doc && typeof doc === "object" ? doc : undefined;
  } catch {
    return undefined;
  }
}

export async function kubeGet(pathname: string): Promise<JsonObject> {
  const explicitUrl = process.env.KUBERNETES_API_URL;
  const host = process.env.KUBERNETES_SERVICE_HOST;
  if (!explicitUrl && !host) throw new Error("クラスタ外のため Kubernetes API は未接続");
  const base = explicitUrl ?? `https://${host}:${process.env.KUBERNETES_SERVICE_PORT_HTTPS ?? "443"}`;
  const url = new URL(pathname, base);
  const [token, ca] = await Promise.all([
    process.env.KUBERNETES_TOKEN
      ? Promise.resolve(process.env.KUBERNETES_TOKEN)
      : readFile(`${SERVICE_ACCOUNT}/token`, "utf8"),
    url.protocol === "https:"
      ? readFile(`${SERVICE_ACCOUNT}/ca.crt`).catch(() => undefined)
      : Promise.resolve(undefined),
  ]);
  const transport = url.protocol === "https:" ? https : http;
  return new Promise((resolve, reject) => {
    const request = transport.request(url, {
      method: "GET",
      ca,
      headers: { Authorization: `Bearer ${token.trim()}`, Accept: "application/json" },
      timeout: 10_000,
    }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode >= 300) {
          reject(new Error(`Kubernetes API ${response.statusCode ?? "?"}: ${body.slice(0, 200)}`));
          return;
        }
        try { resolve(JSON.parse(body)); } catch { reject(new Error("Kubernetes API が不正な JSON を返しました")); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("Kubernetes API timeout")));
    request.on("error", reject);
    request.end();
  });
}

function jobIsRunning(job: JsonObject): boolean {
  if (Number(job.status?.active ?? 0) > 0) return true;
  return Boolean(job.status?.startTime) && !job.status?.completionTime && Number(job.status?.failed ?? 0) === 0;
}

export function parseKubeSnapshot(jobDoc: JsonObject, podDoc: JsonObject, deployment: JsonObject): Pick<KubeSnapshot, "jobs" | "heartReady"> {
  const podByJob = new Map<string, JsonObject>();
  for (const pod of podDoc.items ?? []) {
    const jobName = pod.metadata?.labels?.["job-name"];
    if (jobName) podByJob.set(jobName, pod);
  }
  const jobs = (jobDoc.items ?? [])
    .filter((job: JsonObject) => {
      const kind = job.metadata?.labels?.["heart/kind"];
      return kind && jobIsRunning(job);
    })
    .map((job: JsonObject): RunningJob => {
      const labels = job.metadata.labels ?? {};
      const id = String(job.metadata.name);
      return {
        id,
        role: roleFromKind(String(labels["heart/kind"])),
        projectId: String(labels["heart/project"] ?? "system").toUpperCase(),
        startedAt: String(job.status?.startTime ?? job.metadata.creationTimestamp ?? new Date().toISOString()),
        podPhase: String(podByJob.get(id)?.status?.phase ?? "Pending"),
      };
    })
    .sort((a: RunningJob, b: RunningJob) => a.startedAt.localeCompare(b.startedAt));
  const desired = Number(deployment.spec?.replicas ?? 1);
  const ready = Number(deployment.status?.readyReplicas ?? 0);
  return { jobs, heartReady: desired > 0 && ready >= desired };
}

// 常駐 Deployment 名から役割を推定する。次の住人が増えたとき label だけで
// 載り、ここは best-effort (未知の名前は unknown) でよい
function residentRoleFromName(name: string): AgentRole {
  if (name.includes("heart")) return "heart";
  if (name.includes("core")) return "core";
  return "unknown";
}

export function parseResidents(deploymentListDoc: JsonObject, podDoc: JsonObject): Resident[] {
  const pods: JsonObject[] = podDoc.items ?? [];
  return (deploymentListDoc.items ?? [])
    .filter((d: JsonObject) => String(d.metadata?.labels?.["heart/resident"] ?? "") === "true")
    .map((d: JsonObject): Resident => {
      // pod は ReplicaSet 経由で間接的にしか紐づかないので、pod template の
      // selector label で突き合わせる
      const selector: Record<string, string> = d.spec?.selector?.matchLabels ?? {};
      const pod = pods.find((p) => Object.entries(selector)
        .every(([key, value]) => p.metadata?.labels?.[key] === value));
      return {
        id: String(d.metadata.name),
        role: residentRoleFromName(String(d.metadata.name)),
        replicas: Number(d.spec?.replicas ?? 1),
        readyReplicas: Number(d.status?.readyReplicas ?? 0),
        podPhase: String(pod?.status?.phase ?? "Unknown"),
        startedAt: String(d.metadata.creationTimestamp ?? new Date().toISOString()),
      };
    })
    .sort((a: Resident, b: Resident) => a.id.localeCompare(b.id));
}

export function buildKubeSnapshot(jobDoc: JsonObject, podDoc: JsonObject, heartDoc: JsonObject, residentListDoc: JsonObject): KubeSnapshot {
  return { ...parseKubeSnapshot(jobDoc, podDoc, heartDoc), residents: parseResidents(residentListDoc, podDoc) };
}

export async function getKubeSnapshot(): Promise<KubeSnapshot> {
  try {
    const [jobs, pods, heart, residents, healthCm] = await Promise.all([
      kubeGet(`/apis/batch/v1/namespaces/${NAMESPACE}/jobs?limit=200`),
      kubeGet(`/api/v1/namespaces/${NAMESPACE}/pods?limit=200`),
      kubeGet(`/apis/apps/v1/namespaces/${NAMESPACE}/deployments/autopilot-heart`),
      kubeGet(`/apis/apps/v1/namespaces/${NAMESPACE}/deployments?labelSelector=${encodeURIComponent("heart/resident=true")}`),
      // CPU 飽和前兆 (P-9037)。ConfigMap が無い・RBAC 未適用でもクラスタ観測を
      // 止めない (best-effort の warnings 表示)
      kubeGet(`/api/v1/namespaces/${NAMESPACE}/configmaps/ops-health-report`).catch(() => null),
    ]);
    const snapshot = buildKubeSnapshot(jobs, pods, heart, residents);
    const saturation = saturationWarning(parseReportDoc(healthCm));
    if (saturation) snapshot.warning = saturation;
    return snapshot;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { jobs: [], residents: [], heartReady: false, warning: message };
  }
}


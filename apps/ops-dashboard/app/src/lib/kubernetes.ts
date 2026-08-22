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

export interface KubeSnapshot {
  jobs: RunningJob[];
  heartReady: boolean;
  warning?: string;
}

type JsonObject = Record<string, any>;

async function kubeGet(pathname: string): Promise<JsonObject> {
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

export function parseKubeSnapshot(jobDoc: JsonObject, podDoc: JsonObject, deployment: JsonObject): KubeSnapshot {
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

export async function getKubeSnapshot(): Promise<KubeSnapshot> {
  try {
    const [jobs, pods, heart] = await Promise.all([
      kubeGet(`/apis/batch/v1/namespaces/${NAMESPACE}/jobs?limit=200`),
      kubeGet(`/api/v1/namespaces/${NAMESPACE}/pods?limit=200`),
      kubeGet(`/apis/apps/v1/namespaces/${NAMESPACE}/deployments/autopilot-heart`),
    ]);
    return parseKubeSnapshot(jobs, pods, heart);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { jobs: [], heartReady: false, warning: message };
  }
}


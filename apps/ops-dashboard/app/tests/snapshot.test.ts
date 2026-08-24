import assert from "node:assert/strict";
import test from "node:test";
import { parseKubeSnapshot } from "../src/lib/kubernetes";
import { buildAttention } from "../src/lib/snapshot";

test("Kubernetes の active Job と heart ready を抽出する", () => {
  const result = parseKubeSnapshot({ items: [
    { metadata: { name: "runner-p-0001-a1", labels: { "heart/kind": "runner", "heart/project": "p-0001" }, creationTimestamp: "2026-08-22T10:00:00Z" }, status: { active: 1, startTime: "2026-08-22T10:00:01Z" } },
    { metadata: { name: "reviewer-p-0002-a1", labels: { "heart/kind": "reviewer", "heart/project": "p-0002" } }, status: { completionTime: "2026-08-22T09:00:00Z", succeeded: 1 } },
  ] }, { items: [
    { metadata: { labels: { "job-name": "runner-p-0001-a1" } }, status: { phase: "Running" } },
  ] }, { spec: { replicas: 1 }, status: { readyReplicas: 1 } });
  assert.equal(result.jobs.length, 1);
  assert.equal(result.jobs[0].role, "worker");
  assert.equal(result.jobs[0].projectId, "P-0001");
  assert.equal(result.jobs[0].podPhase, "Running");
  assert.equal(result.heartReady, true);
});

test("stalled、質問待ち、未来の veto window を要対応にする", () => {
  const now = new Date("2026-08-22T10:00:00Z");
  const items = buildAttention([
    { id: "P-0001", title: "質問", state: "stalled", stalled_reason: "session_limit" },
    { id: "P-0002", title: "停止", state: "stalled", stalled_reason: "error" },
    { id: "P-0003", title: "予告", state: "announced", veto_deadline: "2026-08-22T11:00:00Z", irreversible: true },
    { id: "P-0004", title: "期限切れ", state: "announced", veto_deadline: "2026-08-22T09:00:00Z" },
  ], now);
  assert.deepEqual(items.map((item) => item.kind), ["question", "veto", "stalled"]);
  assert.equal(items[1].irreversible, true);
  assert.match(items[1].detail, /veto P-0003/);
});


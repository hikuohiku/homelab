import assert from "node:assert/strict";
import test from "node:test";
import { buildKubeSnapshot, parseReportDoc, parseResidents, saturationWarning } from "../src/lib/kubernetes";

// heart/resident: "true" の Deployment = 常駐エージェント (住人)。
// fixture はクラスタの実応答と同じ形の JSON をインラインで持つ (unit test は API を呼ばない)
const RESIDENT_LIST = { items: [
  {
    metadata: {
      name: "autopilot-core",
      namespace: "autopilot",
      creationTimestamp: "2026-08-23T10:40:00Z",
      labels: { app: "autopilot-core", "heart/resident": "true" },
    },
    spec: { replicas: 1, selector: { matchLabels: { app: "autopilot-core" } } },
    status: { readyReplicas: 1 },
  },
  {
    metadata: {
      name: "autopilot-heart",
      namespace: "autopilot",
      creationTimestamp: "2026-08-22T06:50:00Z",
      labels: { app: "autopilot-heart", "heart/resident": "true" },
    },
    spec: { replicas: 1, selector: { matchLabels: { app: "autopilot-heart" } } },
    status: { readyReplicas: 1 },
  },
  // label 無し = 常駐ではない (退役済みの旧 autopilot など)。列挙に乗らない
  {
    metadata: {
      name: "autopilot",
      namespace: "autopilot",
      creationTimestamp: "2026-07-01T00:00:00Z",
      labels: { app: "autopilot" },
    },
    spec: { replicas: 0, selector: { matchLabels: { app: "autopilot" } } },
    status: {},
  },
] };

const PODS = { items: [
  { metadata: { labels: { app: "autopilot-core" } }, status: { phase: "Running" } },
  { metadata: { labels: { job_name: "other" } }, status: { phase: "Succeeded" } },
] };

test("heart/resident label の Deployment が常駐として列挙され、label 無しは載らない", () => {
  const residents = parseResidents(RESIDENT_LIST, PODS);
  assert.deepEqual(residents.map((r) => r.id), ["autopilot-core", "autopilot-heart"]);
});

test("常駐は Ready 数・Pod phase・開始時刻を持つ", () => {
  const [core, heart] = parseResidents(RESIDENT_LIST, PODS);
  assert.equal(core.replicas, 1);
  assert.equal(core.readyReplicas, 1);
  assert.equal(core.podPhase, "Running");
  assert.equal(core.startedAt, "2026-08-23T10:40:00Z");
  assert.equal(core.role, "core");
  assert.equal(heart.role, "heart");
});

test("pod が見つからない常駐は phase Unknown で沈没しない", () => {
  const residents = parseResidents({ items: [{
    metadata: {
      name: "autopilot-core",
      labels: { app: "autopilot-core", "heart/resident": "true" },
      creationTimestamp: "2026-08-23T10:40:00Z",
    },
    spec: { replicas: 1, selector: { matchLabels: { app: "autopilot-core" } } },
    status: {},
  }] }, { items: [] });
  assert.equal(residents.length, 1);
  assert.equal(residents[0].podPhase, "Unknown");
  assert.equal(residents[0].readyReplicas, 0);
});

test("buildKubeSnapshot は Job 表示と heartReady を保ったまま residents を snapshot に載せる", () => {
  const snapshot = buildKubeSnapshot(
    { items: [
      { metadata: { name: "runner-p-0001-a1", labels: { "heart/kind": "runner", "heart/project": "p-0001" }, creationTimestamp: "2026-08-24T00:00:00Z" }, status: { active: 1, startTime: "2026-08-24T00:00:01Z" } },
    ] },
    PODS,
    { spec: { replicas: 1 }, status: { readyReplicas: 1 } },
    RESIDENT_LIST,
  );
  assert.equal(snapshot.jobs.length, 1);
  assert.equal(snapshot.jobs[0].projectId, "P-0001");
  assert.equal(snapshot.heartReady, true);
  assert.deepEqual(snapshot.residents.map((r) => r.id), ["autopilot-core", "autopilot-heart"]);
});

// P-9037: reporter の latest.json node_saturation キーからの warnings 抽出 (純関数)
const SATURATION_REPORT = {
  node_saturation: {
    status: "warn",
    reasons: ["requests_ratio", "load"],
    requests_m: 3761,
    allocatable_m: 4000,
    requests_ratio: 0.9403,
    load_1m: 25.0,
    vcpus: 4,
    node: "node01",
    load_source: "proc_loadavg",
  },
};

test("saturationWarning は 08-24 実測値 (warn) を数値入り文面にする", () => {
  const warning = saturationWarning(SATURATION_REPORT);
  assert.ok(warning);
  assert.match(warning, /CPU 飽和前兆 \(node01\)/);
  assert.match(warning, /3761m\/4000m/);
  assert.match(warning, /25 > vCPU 4/);
});

test("saturationWarning は ok / キー無し / 観測失敗を出さない", () => {
  assert.equal(saturationWarning(undefined), undefined);
  assert.equal(saturationWarning({}), undefined);
  assert.equal(saturationWarning({ node_saturation: { status: "ok", reasons: [] } }), undefined);
  assert.equal(saturationWarning({ node_saturation: { error: "FileNotFoundError" } }), undefined);
});

test("parseReportDoc は latest.json を解釈し、壊れた入力は undefined", () => {
  assert.deepEqual(
    parseReportDoc({ data: { "latest.json": JSON.stringify(SATURATION_REPORT) } }),
    SATURATION_REPORT,
  );
  assert.equal(parseReportDoc(null), undefined);
  assert.equal(parseReportDoc({}), undefined);
  assert.equal(parseReportDoc({ data: { "latest.json": "not json" } }), undefined);
});

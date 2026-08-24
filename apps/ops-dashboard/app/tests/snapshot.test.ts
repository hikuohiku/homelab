import assert from "node:assert/strict";
import test from "node:test";
import { parseKubeSnapshot } from "../src/lib/kubernetes";
import { projectsFromCrs, stopEngagedFromCr } from "../src/lib/ops-state";
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

// 読み先が ops-state の projects.json から Project CR に移った (4b-2a)。
// git 版 (mergeArchive) と同じ答えになることを固定する
test("Project CR から projects.json と同じプロジェクト一覧を組み立てる", () => {
  const projects = projectsFromCrs({ items: [
    // spec.spec は立案時の spec。題名と不可逆の穴埋めにだけ使い、ボードには載せない
    { spec: { id: "P-0002", title: "", state: "active", spec: { title: "台帳の題名", irreversible: true } } },
    { spec: { id: "P-0001", title: "そのまま", state: "delivered", irreversible: false } },
    // 棄却案は selector で外れるが、届いても混ぜない (250 件超の終端の山)
    { spec: { id: "P-0900", title: "棄却", state: "rejected" } },
    // 突き合わせの鍵が無いものは落とす
    { spec: { id: "", title: "", state: "proposed" } },
    {},
  ] });
  assert.deepEqual(projects.map((p) => p.id), ["P-0001", "P-0002"]);
  assert.equal(projects[1].title, "台帳の題名");
  assert.equal(projects[1].irreversible, true);
  assert.equal(projects[0].irreversible, false);
  assert.equal("spec" in projects[1], false);
});


// 「止めて」の置き場が ops-state の projects.json から HeartState CR に移った
// (4b-2b)。git 側は凍った古い値なので、ここを間違えると停止が画面に出なくなる
test("HeartState CR から stop_engaged を読む", () => {
  assert.equal(stopEngagedFromCr({ spec: { stop_engaged: true } }), true);
  assert.equal(stopEngagedFromCr({ spec: { stop_engaged: false } }), false);
  // CR がまだ無い / spec が空 = まだ誰も止めていない
  assert.equal(stopEngagedFromCr({}), false);
  assert.equal(stopEngagedFromCr({ spec: {} }), false);
});

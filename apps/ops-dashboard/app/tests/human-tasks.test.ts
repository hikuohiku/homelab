// ops/tools/human_tasks.py の mirror テスト (P-0272)。
//
// dashboard runtime に python3 は無いので、同じルールを TypeScript に移植している。
// fixture は Python 側テストと**この 1 ファイルを共有**
// (ops/tests/fixtures/human_tasks_seeds.md)。同入力 → 同出力を両言語で固定し、
// 片側だけの仕様変更 (drift) を防ぐ。項目を変えるときは両側の期待値も一緒に直すこと。

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { backlogCreatedIndex, parseHumanTasks } from "../src/lib/human-tasks";

// tests/ の 4 つ上 = リポジトリルート
const FIXTURE_URL = new URL("../../../../ops/tests/fixtures/human_tasks_seeds.md", import.meta.url);
const TODAY = new Date("2026-08-24T12:00:00Z");

test("bullet の T 項目だけを抽出する (番号付き・取り消し線・節の外は除外)", async () => {
  const seeds = await readFile(FIXTURE_URL, "utf8");
  const tasks = parseHumanTasks(seeds, {}, TODAY);
  assert.deepEqual(tasks.map((task) => task.id), ["T-0107", "T-0140", "T-0141", "T-0148"]);
  for (const task of tasks) {
    assert.deepEqual(Object.keys(task).sort(), ["ageDays", "id", "title"]);
    assert.equal(task.ageDays, 0);
  }
});

test("backlog の created を join して古い順に並べる (欠落は 0 で最後)", async () => {
  const seeds = await readFile(FIXTURE_URL, "utf8");
  const tasks = parseHumanTasks(seeds, {
    "T-0107": "2026-07-01",
    "T-0141": "2026-08-06",
    "T-0140": "2026-08-20",
  }, TODAY);
  // Python 側 (ops/tests/test_human_tasks.py) と同じ期待値
  assert.deepEqual(
    tasks.map((task) => [task.id, task.ageDays]),
    [["T-0107", 54], ["T-0141", 18], ["T-0140", 4], ["T-0148", 0]],
  );
  assert.equal(tasks[0].created, "2026-07-01");
  assert.equal("created" in tasks[3], false);
});

test("形式外の created と未来日は age_days 0", async () => {
  const seeds = await readFile(FIXTURE_URL, "utf8");
  const tasks = parseHumanTasks(seeds, { "T-0107": "2026/08/06" }, TODAY);
  assert.ok(tasks.every((task) => task.ageDays === 0));
  assert.ok(tasks.every((task) => !("created" in task)));
});

test("seeds に節が無ければ空配列", () => {
  assert.deepEqual(parseHumanTasks("# seeds\n\n- T-0107: 単なる bullet\n", {}, TODAY), []);
});

test("backlog JSON を id→created の索引にする (壊れていれば空)", () => {
  assert.deepEqual(
    backlogCreatedIndex(JSON.stringify({ tasks: [
      { id: "T-0107", status: "needs-human", created: "2026-08-06" },
      { id: "T-0140", created: "2026-08-06" },
    ] })),
    { "T-0107": "2026-08-06", "T-0140": "2026-08-06" },
  );
  assert.deepEqual(backlogCreatedIndex("not json"), {});
  assert.deepEqual(backlogCreatedIndex("[1, 2, 3]"), {});
});

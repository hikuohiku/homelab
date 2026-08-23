import assert from "node:assert/strict";
import test from "node:test";
import { toRemindersView } from "../src/lib/reminders";

// P-0231: 「次の予定」節への入力変換。due 計算は Python 側 (ops/life/reminders.py)
// の専権なので、ここで見るのは欠損の吸収と空判定だけ

test("取得した断片をそのまま節の本文にする", () => {
  const view = toRemindersView("明日 8/24 ゴミ収集\n明後日 8/25 支払日");
  assert.equal(view.empty, false);
  assert.equal(view.body, "明日 8/24 ゴミ収集\n明後日 8/25 支払日");
});

test("前後の空白は落とす (git show の末尾改行)", () => {
  const view = toRemindersView("今日 8/24 防災の日\n");
  assert.equal(view.body, "今日 8/24 防災の日");
});

test("ファイルが無い・取得失敗は空扱い。節は消さないため empty で区別する", () => {
  for (const raw of [undefined, null, "", "   \n"]) {
    const view = toRemindersView(raw);
    assert.equal(view.empty, true, String(raw));
    assert.equal(view.body, "");
  }
});

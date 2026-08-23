// P-0231: ops-state ブランチの briefing/reminders.txt (描画済み断片) を
// 「次の予定」節に映すための変換。48h の due 計算と文面は Python 側
// (ops/life/reminders.py) の専権で、ここでは取得失敗の吸収と空判定だけを行う
// (同じ事実を 2 箇所で計算しない)。

export interface RemindersView {
  body: string;
  empty: boolean;
}

export function toRemindersView(raw?: string | null): RemindersView {
  const body = typeof raw === "string" ? raw.trim() : "";
  return { body, empty: body.length === 0 };
}

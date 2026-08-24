"use client";

// プロジェクトへの介入ボタン。issue #56 へコメントしに行かなくても、この画面から
// 拒否・承認・既読化ができる。
//
// 送信先は書き置きと同じ /api/feedback (NATS 経由)。heart の triage が
// "veto P-NNNN" / "approve P-NNNN" / "ack P-NNNN" を決定論で拾うので、専用の API も
// 権限も要らない。この経路は heart に依存しない — heart が死んでいても意思は
// JetStream に残り、復帰した heart が読む。
//
// 押し間違いが取り返しのつかない操作になるので、2 段階にしてある (押す → 確定)。
// 反映は heart のビート (最長 1 分) 後。押した直後に画面が変わらないのは正常。

import { useState } from "react";

export type ProjectAction = "veto" | "approve" | "ack";

const LABEL: Record<ProjectAction, string> = {
  veto: "拒否する",
  approve: "承認して実行",
  ack: "確認済みにする",
};

// 確定ボタンに出す文言。何が起きるかを動詞で言う
const CONFIRM: Record<ProjectAction, string> = {
  veto: "中止する",
  approve: "いま実行する",
  ack: "キューから下げる",
};

const NOTE: Record<ProjectAction, string> = {
  veto: "このプロジェクトを中止します。走行中なら Job も止まります。",
  approve: "拒否期限を待たずに着手します。予算遮断と並列上限は従来どおり効きます。",
  ack: "要対応キューから下げます。状態と履歴はそのまま残ります。",
};

type SendState =
  | { kind: "idle" }
  | { kind: "confirming"; action: ProjectAction }
  | { kind: "sending" }
  | { kind: "done"; action: ProjectAction }
  | { kind: "error"; message: string };

export default function ProjectActions({
  projectId,
  actions,
}: {
  projectId: string;
  actions: ProjectAction[];
}) {
  const [state, setState] = useState<SendState>({ kind: "idle" });

  async function send(action: ProjectAction) {
    setState({ kind: "sending" });
    try {
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // triage が拾う形そのもの。余計な地の文を付けない
        body: JSON.stringify({ body: `${action} ${projectId}` }),
      });
      const payload = (await response.json()) as { id?: string; error?: string };
      if (response.ok && payload.id) {
        setState({ kind: "done", action });
      } else {
        setState({ kind: "error", message: payload.error ?? `送信に失敗 (${response.status})` });
      }
    } catch (error) {
      setState({ kind: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }

  if (state.kind === "done") {
    return (
      <div className="actions actions--done">
        <span>受け付けました（{LABEL[state.action]}）</span>
        <p>heart の次のビートで反映されます（最長 1 分）。</p>
      </div>
    );
  }

  if (state.kind === "confirming") {
    return (
      <div className={`actions actions--confirm actions--confirm-${state.action}`}>
        <p>{NOTE[state.action]}</p>
        <div className="actions__row">
          <button type="button" className={`actions__go actions__go--${state.action}`} onClick={() => send(state.action)}>
            {CONFIRM[state.action]}
          </button>
          <button type="button" className="actions__cancel" onClick={() => setState({ kind: "idle" })}>
            やめる
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="actions">
      <div className="actions__row">
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className={`actions__btn actions__btn--${action}`}
            disabled={state.kind === "sending"}
            onClick={() => setState({ kind: "confirming", action })}
          >
            {LABEL[action]}
          </button>
        ))}
      </div>
      {state.kind === "sending" && <p className="actions__pending">送信中…</p>}
      {state.kind === "error" && <p className="actions__err">{state.message}</p>}
    </div>
  );
}

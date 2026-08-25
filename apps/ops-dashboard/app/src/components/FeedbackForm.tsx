"use client";

// 書き置きフォーム。旧ダッシュボードの feedback フォームの後継。
// 投稿は /api/feedback → NATS → heart (次のビートで読まれる)。

import { useState } from "react";

type SendState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "done"; id: string }
  | { kind: "error"; message: string };

export default function FeedbackForm() {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState("");
  const [state, setState] = useState<SendState>({ kind: "idle" });

  async function submit() {
    if (!body.trim() || state.kind === "sending") return;
    setState({ kind: "sending" });
    try {
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      });
      const payload = (await response.json()) as { id?: string; error?: string };
      if (response.ok && payload.id) {
        setBody("");
        setState({ kind: "done", id: payload.id });
      } else {
        setState({ kind: "error", message: payload.error ?? `送信に失敗 (${response.status})` });
      }
    } catch (error) {
      setState({ kind: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <div className="feedback">
      {open ? (
        <div className="feedback__panel">
          <div className="feedback__head">
            <span>書き置き — エージェントへのフィードバック</span>
            <button type="button" className="feedback__close" onClick={() => setOpen(false)} aria-label="閉じる">
              ×
            </button>
          </div>
          <textarea
            value={body}
            onChange={(event) => setBody(event.target.value)}
            maxLength={20000}
            rows={4}
            placeholder="気づいたこと・不満・veto P-NNNN など。次の起動で必ず読まれます"
          />
          <div className="feedback__foot">
            {state.kind === "done" && <span className="feedback__ok">保存しました ({state.id})</span>}
            {state.kind === "error" && <span className="feedback__err">{state.message}</span>}
            <button type="button" onClick={submit} disabled={state.kind === "sending" || !body.trim()}>
              {state.kind === "sending" ? "送信中…" : "送る"}
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="feedback__toggle" onClick={() => setOpen(true)}>
          ✍ 書き置き
        </button>
      )}
    </div>
  );
}

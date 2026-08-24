"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AgentRole, AgentSnapshot, Project, Snapshot, TranscriptEvent } from "@/lib/types";
import { mergeTranscriptEvent } from "@/lib/transcript-client";
import ProjectActions, { type ProjectAction } from "@/components/ProjectActions";

type View = "live" | "projects" | "attention";

// 要対応キューの種別ごとに出す介入ボタン。
// veto = 予告中（窓待ち）なので承認と拒否、それ以外は既読化だけ
const QUEUE_ACTIONS: Record<string, ProjectAction[]> = {
  veto: ["approve", "veto"],
  stalled: ["ack"],
  question: ["ack"],
};

const STATE_LABEL: Record<string, string> = {
  proposed: "立案", announced: "予告", active: "実行", in_review: "レビュー",
  merging: "取り込み", soaking: "様子見", delivered: "納品", stalled: "停止", vetoed: "拒否",
};
const ROLE_LABEL: Record<AgentRole, string> = {
  worker: "WORKER", reviewer: "REVIEWER", curriculum: "CURRICULUM",
  critic: "CRITIC", consolidation: "CONSOLIDATION", chore: "CHORE",
  core: "CORE", heart: "HEART", unknown: "AGENT",
};
const LIVE_STATES = ["proposed", "announced", "active", "in_review", "merging", "soaking"];

function elapsed(start: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.floor((now - Date.parse(start)) / 1000));
  if (seconds < 60) return `${seconds}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`;
  return `${Math.floor(seconds / 3600)}時間 ${Math.floor((seconds % 3600) / 60)}分`;
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat("ja-JP", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

// 表示時刻はすべて JST (2026-08-22 利用者指示)。サーバ/データは UTC のまま
function formatDate(value?: string): string {
  if (!value) return "観測なし";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(value));
}

function jstClock(value?: string | number): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

function countdown(value?: string, now = Date.now()): string {
  if (!value) return "期限なし";
  const seconds = Math.max(0, Math.floor((Date.parse(value) - now) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return hours > 0 ? `${hours}時間 ${minutes}分` : `${minutes}分 ${String(secs).padStart(2, "0")}秒`;
}

function AgentCard({ agent, active, now, onSelect }: {
  agent: AgentSnapshot; active: boolean; now: number; onSelect: () => void;
}) {
  return (
    <button className={`agent-card ${active ? "is-active" : ""}`} onClick={onSelect} aria-pressed={active}>
      <span className="agent-card__top">
        <span className="agent-card__labels">
          <span className={`role role--${agent.role}`}>{ROLE_LABEL[agent.role]}</span>
          {agent.resident && <span className="resident-badge">常駐</span>}
        </span>
        <span className="live-dot"><i />{agent.podPhase === "Running" ? "LIVE" : agent.podPhase}</span>
      </span>
      <strong>{agent.projectId}</strong>
      <span className="agent-card__title">{agent.projectTitle ?? "システム運用"}</span>
      <span className="agent-card__action">› {agent.recentAction}</span>
      {/* 常駐は長時間稼働が普通なので経過時間でなく開始時刻を出す */}
      <span className="agent-card__time">{agent.resident ? `開始 ${formatDate(agent.startedAt)}` : `T+ ${elapsed(agent.startedAt, now)}`}</span>
    </button>
  );
}

function ToolEvent({ event }: { event: TranscriptEvent }) {
  const command = event.input && typeof event.input === "object" && "command" in event.input
    ? String((event.input as { command: unknown }).command)
    : event.input == null ? "" : JSON.stringify(event.input, null, 2);
  const output = typeof event.output === "string" ? event.output :
    event.output == null ? "" : JSON.stringify(event.output, null, 2);
  return (
    <details className={`tool-event tool-event--${event.status ?? "running"}`} open={event.status === "running"}>
      <summary>
        <span className="tool-glyph">$</span>
        <span>{event.toolName ?? "tool"}</span>
        <span className="tool-status">
          {event.at && <span className="event-clock">{jstClock(event.at)} </span>}
          {event.status === "completed" ? "完了" : event.status === "failed" ? "失敗" : "実行中"}
        </span>
      </summary>
      {command && <pre className="tool-command">{command}</pre>}
      {output && <pre className="tool-output">{output}</pre>}
    </details>
  );
}

function TranscriptLine({ event }: { event: TranscriptEvent }) {
  if (event.kind === "tool") return <ToolEvent event={event} />;
  if (event.kind === "thinking") {
    return (
      <details className="thought-event">
        <summary>思考を見る</summary>
        <div>{event.text || "…"}</div>
      </details>
    );
  }
  if (event.kind === "result") {
    return (
      <div className="result-event">
        {/* 1 回の LLM 呼び出し (ステップ) の消費。累計はフッターに出る */}
        <span>このステップの消費</span>
        <strong>{compactNumber(event.usage?.total ?? 0)} tokens</strong>
        <span>${(event.usage?.costUsd ?? 0).toFixed(4)}</span>
        {event.at && <span className="event-clock">{jstClock(event.at)}</span>}
      </div>
    );
  }
  return (
    <article className={`message-event message-event--${event.kind}`}>
      <span className="event-rail" />
      <div className="message-event__meta">
        {event.kind === "error" ? "ERROR" : event.kind === "system" ? "SYSTEM" : "AGENT"}
        {event.at && <span className="event-clock"> {jstClock(event.at)}</span>}
      </div>
      <div className="message-event__body">{event.text || "…"}</div>
    </article>
  );
}

function TranscriptViewer({ agent }: { agent?: AgentSnapshot }) {
  const [events, setEvents] = useState<TranscriptEvent[]>([]);
  const [status, setStatus] = useState("接続中");
  const [following, setFollowing] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  const agentId = agent?.id;
  useEffect(() => {
    setEvents([]);
    setFollowing(true);
    usageMapRef.current = new Map();
    setUsage({ tokens: 0, cost: 0 });
    if (!agentId) return;
    const source = new EventSource(`/api/agents/${encodeURIComponent(agentId)}/events`);
    // セッション切替 (reset) で画面を消さない。短いセッションを連ねる worker だと
    // 数分ごとに全消去され「ずっと空」に見える (2026-08-22 利用者報告)。
    // 区切り行を挟んで継続し、直近 500 イベントだけ保持する
    const onReset = (message: MessageEvent<string>) => {
      const data = JSON.parse(message.data) as { file?: string };
      setEvents((current) => current.length === 0 ? current : [...current, {
        id: `session-break-${data.file ?? Date.now()}`,
        kind: "system" as const,
        text: `―― セッション切替 (${data.file ?? "?"}) ――`,
      }].slice(-500));
    };
    const onTranscript = (message: MessageEvent<string>) => {
      const incoming = JSON.parse(message.data) as TranscriptEvent;
      setEvents((current) => mergeTranscriptEvent(current, incoming).slice(-500));
      if (incoming.usage && incoming.id) {
        usageMapRef.current.set(incoming.id, {
          tokens: incoming.usage.total ?? 0, cost: incoming.usage.costUsd ?? 0,
        });
        let tokens = 0; let cost = 0;
        for (const entry of usageMapRef.current.values()) { tokens += entry.tokens; cost += entry.cost; }
        setUsage({ tokens, cost });
      }
      setStatus("LIVE");
    };
    const onStatus = (message: MessageEvent<string>) => {
      const data = JSON.parse(message.data) as { message?: string };
      setStatus(data.message ?? "待機中");
    };
    const onStreamError = () => setStatus("再接続中");
    source.addEventListener("reset", onReset as EventListener);
    source.addEventListener("transcript", onTranscript as EventListener);
    source.addEventListener("status", onStatus as EventListener);
    source.addEventListener("stream-error", onStreamError);
    source.onerror = () => setStatus("再接続中");
    return () => source.close();
    // agent オブジェクトは 10 秒ごとの snapshot ポーリングで毎回作り直される。
    // 参照で依存すると同じエージェントでも 10 秒ごとに SSE 張り直し + イベント
    // 全消去 + スクロールリセットになる (2026-08-22 利用者報告)。id で依存する
  }, [agentId]);

  useEffect(() => {
    if (following && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [events, following]);

  // 表示バッファ (直近 500 件) と独立に、開いてから見た usage を id 重複排除で
  // 積み上げる。バッファ頭打ちで古い step_finish が落ちると合計が減って見えた
  // (2026-08-22 利用者報告)。SSE 再接続の再送も id で二重計上しない
  const usageMapRef = useRef(new Map<string, { tokens: number; cost: number }>());
  const [usage, setUsage] = useState({ tokens: 0, cost: 0 });

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight < 72);
  };

  return (
    <section className="scope" aria-label="会話ストリーム">
      <header className="scope__header">
        <div>
          <span className="scope__eyebrow">LIVE TRANSCRIPT / READ ONLY</span>
          <h2>{agent ? `${agent.projectId} · ${ROLE_LABEL[agent.role]}` : "エージェント待機中"}</h2>
        </div>
        <span className="stream-status"><i />{status}</span>
      </header>
      <div className="scope__screen" ref={scrollRef} onScroll={onScroll}>
        {events.length === 0 ? (
          // 常駐も P-9004 で transcripts/resident/<role>.jsonl を持つようになったので、
          // 「常駐エージェントのため表示なし」(P-9003) をやめ、Job と同じ待機表示に戻す
          <div className="scope__empty">
            <strong>{agent ? "transcript 信号を待っています" : "走行中の Job はありません"}</strong>
            <span>{agent ? "ファイルが作られると自動で表示します" : "次の起動時にここへ会話が流れます"}</span>
          </div>
        ) : events.map((event, index) => <TranscriptLine key={`${event.id}-${index}`} event={event} />)}
      </div>
      {!following && (
        <button className="follow-button" onClick={() => setFollowing(true)}>↓ 最新へ追尾</button>
      )}
      <footer className="scope__footer">
        <span>{events.length} EVENTS</span>
        <span>INPUT + OUTPUT <strong>{compactNumber(usage.tokens)}</strong></span>
        <span>COST <strong>${usage.cost.toFixed(4)}</strong></span>
        <span className={following ? "following" : ""}>{following ? "● AUTO FOLLOW" : "○ FOLLOW PAUSED"}</span>
      </footer>
    </section>
  );
}

function EmptyShift({ snapshot, now }: { snapshot: Snapshot; now: number }) {
  const delivered = [...snapshot.projects]
    .filter((project) => project.state === "delivered")
    .sort((a, b) => String(b.merging_since ?? "").localeCompare(String(a.merging_since ?? "")))[0];
  const veto = snapshot.attention.find((item) => item.kind === "veto");
  return (
    <section className="quiet-shift">
      <div className="quiet-shift__signal"><span /> ALL CHANNELS QUIET</div>
      <h2>現在、走行中のエージェントはありません。</h2>
      <div className="quiet-shift__facts">
        <div><span>直近の納品</span><strong>{delivered?.id ?? "—"}</strong><p>{delivered?.title ?? "納品記録なし"}</p></div>
        <div><span>次の拒否期限</span><strong>{veto ? countdown(veto.deadline, now) : "窓なし"}</strong><p>{veto ? `${veto.id} · ${veto.title}` : "確認が必要な予告はありません"}</p></div>
      </div>
    </section>
  );
}

function ProjectCard({ project, now }: { project: Project; now: number }) {
  // 消費量は上限のない計測値 (2026-08-24 に soft cap 廃止)。棒グラフは出さない
  const used = project.budget?.used_tokens ?? 0;
  return (
    <article className={`project-card ${project.irreversible ? "project-card--irreversible" : ""}`}>
      <div className="project-card__id">
        <strong>{project.id}</strong>
        {project.irreversible && <span>不可逆</span>}
      </div>
      <h3>{project.title}</h3>
      {project.veto_deadline && project.state === "announced" && (
        <div className="project-card__deadline">拒否期限まで {countdown(project.veto_deadline, now)}</div>
      )}
      {project.stalled_reason && <div className="project-card__reason">{project.stalled_reason}</div>}
      {project.state === "announced" && <ProjectActions projectId={project.id} actions={["approve", "veto"]} />}
      <footer>
        <span>{compactNumber(used)} tokens</span>
        {project.prs?.length ? <span>PR #{project.prs.at(-1)}</span> : null}
      </footer>
    </article>
  );
}

function ProjectBoard({ projects, now }: { projects: Project[]; now: number }) {
  const live = LIVE_STATES.map((state) => ({ state, projects: projects.filter((project) => project.state === state) }));
  const delivered = projects.filter((project) => project.state === "delivered");
  const closed = projects.filter((project) => project.state === "stalled" || project.state === "vetoed");
  return (
    <section className="board" aria-labelledby="board-title">
      <div className="section-heading">
        <div><span>STATE PIPELINE</span><h2 id="board-title">プロジェクトボード</h2></div>
        <p>状態は左から右へ進みます。終端は視界から畳んでいます。</p>
      </div>
      <div className="pipeline">
        {live.map((column, index) => (
          <section className="pipeline__column" key={column.state}>
            <header><span>{String(index + 1).padStart(2, "0")}</span><strong>{STATE_LABEL[column.state]}</strong><em>{column.projects.length}</em></header>
            <div className="pipeline__track" />
            {column.projects.length ? column.projects.map((project) => <ProjectCard key={project.id} project={project} now={now} />)
              : <div className="pipeline__empty">—</div>}
          </section>
        ))}
      </div>
      <div className="terminal-folds">
        <details><summary><span>納品済み</span><strong>{delivered.length}</strong></summary><div className="terminal-grid">{delivered.map((p) => <ProjectCard key={p.id} project={p} now={now} />)}</div></details>
        <details><summary><span>停止・拒否</span><strong>{closed.length}</strong></summary><div className="terminal-grid">{closed.map((p) => <ProjectCard key={p.id} project={p} now={now} />)}</div></details>
      </div>
    </section>
  );
}

function AttentionQueue({ snapshot, now }: { snapshot: Snapshot; now: number }) {
  return (
    <section className="attention" aria-labelledby="attention-title">
      <div className="section-heading">
        <div><span>OPERATOR QUEUE</span><h2 id="attention-title">要対応キュー</h2></div>
        <p>この画面から拒否・承認・既読化ができます。反映は heart の次のビート（最長 1 分）。</p>
      </div>
      {snapshot.heart.stale && (
        <div className="heart-alarm"><span>HEART SIGNAL LOST</span><strong>最終心拍から 5 分以上経過</strong><p>最終観測: {formatDate(snapshot.heart.at)}</p></div>
      )}
      {snapshot.attention.length === 0 ? <div className="queue-empty">対応待ちはありません。</div> : (
        <div className="queue-list">
          {snapshot.attention.map((item) => (
            <article className={`queue-item queue-item--${item.kind}`} key={`${item.kind}-${item.id}`}>
              <div className="queue-item__kind">{item.kind === "veto" ? "VETO WINDOW" : item.kind === "question" ? "QUESTION" : "STALLED"}</div>
              <div><strong>{item.id}</strong><h3>{item.title}</h3><p>{item.detail}</p></div>
              <div className="queue-item__deadline">{item.deadline ? <><span>残り</span><strong>{countdown(item.deadline, now)}</strong></> : <strong>要確認</strong>}</div>
              {item.irreversible && <span className="irreversible-flag">不可逆操作を含む</span>}
              <ProjectActions projectId={item.id} actions={QUEUE_ACTIONS[item.kind] ?? ["ack"]} />
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [loadError, setLoadError] = useState("");
  const [view, setView] = useState<View>("live");
  const [selectedId, setSelectedId] = useState<string>();
  const [now, setNow] = useState(Date.now());

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/snapshot", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const value = await response.json() as Snapshot;
      setSnapshot(value);
      setLoadError("");
      setSelectedId((current) => value.agents.some((agent) => agent.id === current) ? current : value.agents[0]?.id);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    void load();
    const refresh = setInterval(() => void load(), 10_000);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(refresh); clearInterval(clock); };
  }, [load]);

  const selected = snapshot?.agents.find((agent) => agent.id === selectedId);
  const heartBad = snapshot?.heart.stale || snapshot?.heart.stopEngaged || !snapshot?.heart.deploymentReady;

  return (
    <main>
      <header className="masthead">
        <a className="identity" href="#top" aria-label="Mission Control ホーム">
          <span className="identity__mark">MC</span>
          <span><strong>MISSION CONTROL</strong><small>HOMELAB AUTOPILOT / NODE01</small></span>
        </a>
        <nav aria-label="画面切り替え">
          <button className={view === "live" ? "active" : ""} onClick={() => setView("live")}><span>01</span>エージェント・ライブ</button>
          <button className={view === "projects" ? "active" : ""} onClick={() => setView("projects")}><span>02</span>プロジェクト</button>
          <button className={view === "attention" ? "active" : ""} onClick={() => setView("attention")}><span>03</span>要対応 {snapshot?.attention.length ? <em>{snapshot.attention.length}</em> : null}</button>
          <a className="nav-page" href="/architecture" title="構成図ページを開く"><span>↗</span>構成図</a>
        </nav>
        <div className={`heart-chip ${heartBad ? "heart-chip--bad" : ""}`}>
          <i /><span><small>HEART / BEAT {snapshot?.heart.beat ?? "—"}</small><strong>{heartBad ? "要確認" : "正常"}</strong></span>
        </div>
      </header>

      <div className="status-line" id="top">
        <span>JST {jstClock(now)}</span>
        <span>RUNNING <strong>{snapshot?.agents.length ?? "—"}</strong></span>
        <span>TODAY <strong>{snapshot ? compactNumber(snapshot.todayTokens) : "—"}</strong> tokens / {snapshot?.todaySessions ?? "—"} sessions{snapshot && snapshot.todayEmptySessions > 0 ? ` (${snapshot.todayEmptySessions} empty)` : ""} / <strong>${snapshot?.todayCostUsd.toFixed(2) ?? "—"}</strong></span>
        <span>LAST HEART {formatDate(snapshot?.heart.at)}</span>
        <span className="status-line__readonly">READ ONLY</span>
      </div>

      {loadError && <div className="global-warning">状態を更新できません: {loadError}</div>}
      {snapshot?.warnings.map((warning) => <div className="global-warning" key={warning}>{warning}</div>)}

      {!snapshot ? <div className="loading"><span />管制信号を同期中</div> : (
        <>
          {view === "live" && (
            <div className="live-view">
              {snapshot.agents.length === 0 ? <EmptyShift snapshot={snapshot} now={now} /> : (
                <section className="agents-strip" aria-label="走行中エージェント">
                  <div className="agents-strip__heading"><span>ACTIVE CHANNELS</span><strong>{snapshot.agents.length}</strong></div>
                  <div className="agents-strip__cards">
                    {snapshot.agents.map((agent) => <AgentCard key={agent.id} agent={agent} active={selectedId === agent.id} now={now} onSelect={() => setSelectedId(agent.id)} />)}
                  </div>
                </section>
              )}
              <TranscriptViewer agent={selected} />
            </div>
          )}
          {view === "projects" && <ProjectBoard projects={snapshot.projects} now={now} />}
          {view === "attention" && <AttentionQueue snapshot={snapshot} now={now} />}
        </>
      )}
    </main>
  );
}

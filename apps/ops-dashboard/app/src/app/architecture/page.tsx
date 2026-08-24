import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "構成図 — Mission Control",
  description:
    "heart-and-projects の全体像。heart のビート、プロジェクトの一生、状態の層、人間との接点を 1 ページで。",
};

function BeatDiagram() {
  return (
    <svg className="arch-svg" viewBox="0 0 900 250" role="img" aria-label="heart のビート: 観測から状態機械、Job 起動へ進み、120 秒ごとに繰り返す循環図">
      <title>heart のビート (120 秒周期の決定論ループ)</title>
      <defs>
        <marker id="arr-beat" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-head arch-head--signal" />
        </marker>
      </defs>
      <rect x={30} y={40} width={240} height={90} rx={2} className="arch-node arch-node--accent" />
      <text x={150} y={80} textAnchor="middle" className="arch-title">観測</text>
      <text x={150} y={104} textAnchor="middle" className="arch-sub">K8s / ArgoCD / Proxmox を読む</text>
      <line x1={272} y1={85} x2={318} y2={85} className="arch-edge--signal" markerEnd="url(#arr-beat)" />
      <rect x={330} y={40} width={240} height={90} rx={2} className="arch-node arch-node--accent" />
      <text x={450} y={80} textAnchor="middle" className="arch-title">状態機械</text>
      <text x={450} y={104} textAnchor="middle" className="arch-sub">Project CR を前進させる</text>
      <line x1={572} y1={85} x2={618} y2={85} className="arch-edge--signal" markerEnd="url(#arr-beat)" />
      <rect x={630} y={40} width={240} height={90} rx={2} className="arch-node arch-node--accent" />
      <text x={750} y={80} textAnchor="middle" className="arch-title">Job を起こす</text>
      <text x={750} y={104} textAnchor="middle" className="arch-sub">必要なセッションだけ起動</text>
      <path d="M 745 132 V 200 H 150 V 136" fill="none" className="arch-edge--signal" markerEnd="url(#arr-beat)" />
      <text x={450} y={190} textAnchor="middle" className="arch-note-text">120 秒ごとに再び — LLM は呼ばない</text>
    </svg>
  );
}

const LIFE_STAGES = [
  { num: "01", name: "立案", sub: "VISION との差分から", tone: "blue" },
  { num: "02", name: "採択ゲート", sub: "人間が選んで動く", tone: "amber" },
  { num: "03", name: "予告", sub: "拒否権つきの告知", tone: "amber" },
  { num: "04", name: "実行", sub: "runner の連鎖で遂行", tone: "accent" },
  { num: "05", name: "レビュー", sub: "本人以外が検査する", tone: "accent" },
  { num: "06", name: "取り込み", sub: "Git → CI → ArgoCD", tone: "accent" },
  { num: "07", name: "様子見", sub: "本番で静かに観る", tone: "accent" },
  { num: "08", name: "納品", sub: "homelab への差分", tone: "blue" },
] as const;

function LifeStage({ stage, x, y }: { stage: (typeof LIFE_STAGES)[number]; x: number; y: number }) {
  return (
    <g>
      <rect x={x} y={y} width={186} height={84} rx={2} className={`arch-node arch-node--${stage.tone}`} />
      <text x={x + 12} y={y + 22} className="arch-num">{stage.num}</text>
      <text x={x + 12} y={y + 46} className="arch-title">{stage.name}</text>
      <text x={x + 12} y={y + 66} className="arch-sub">{stage.sub}</text>
    </g>
  );
}

function LifeDiagram() {
  return (
    <svg className="arch-svg" viewBox="0 0 900 250" role="img" aria-label="プロジェクトの一生: 立案から採択ゲート、予告、実行、レビュー、取り込み、様子見、納品までの 8 段階">
      <title>プロジェクトの一生 (curriculum → 採択 → 予告 → 実行 → レビュー → merge → soak → 納品)</title>
      <defs>
        <marker id="arr-life" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-head arch-head--muted" />
        </marker>
      </defs>
      {/* 上段は左から右へ */}
      <LifeStage stage={LIFE_STAGES[0]} x={15} y={15} />
      <LifeStage stage={LIFE_STAGES[1]} x={238} y={15} />
      <LifeStage stage={LIFE_STAGES[2]} x={461} y={15} />
      <LifeStage stage={LIFE_STAGES[3]} x={684} y={15} />
      <line x1={205} y1={57} x2={234} y2={57} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <line x1={428} y1={57} x2={457} y2={57} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <line x1={651} y1={57} x2={680} y2={57} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      {/* 下段は右から左へ続く */}
      <path d="M 777 103 V 139" fill="none" className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <LifeStage stage={LIFE_STAGES[4]} x={684} y={145} />
      <LifeStage stage={LIFE_STAGES[5]} x={461} y={145} />
      <LifeStage stage={LIFE_STAGES[6]} x={238} y={145} />
      <LifeStage stage={LIFE_STAGES[7]} x={15} y={145} />
      <line x1={680} y1={187} x2={651} y2={187} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <line x1={457} y1={187} x2={428} y2={187} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <line x1={234} y1={187} x2={205} y2={187} className="arch-edge--muted" markerEnd="url(#arr-life)" />
      <text x={450} y={244} textAnchor="middle" className="arch-note-text">採択ゲートは人間の手にある。レビューと verify の実測だけが次段へ通す</text>
    </svg>
  );
}

const STATE_LAYERS = [
  { name: "Project CR (クラスタ)", desc: "プロジェクトの正。書き手は heart のみ (RBAC で強制)。restic で B2 へ backup", tone: "accent" },
  { name: "main ブランチ", desc: "コードと manifest の正体。変更は Git → CI → ArgoCD の一本道だけ", tone: "blue" },
  { name: "PVC autopilot-data", desc: "transcript の生ログ。エピソード記憶であり失敗の教師信号になる", tone: "amber" },
] as const;

function StateLayers() {
  return (
    <svg className="arch-svg" viewBox="0 0 900 240" role="img" aria-label="状態の置き場所: Project CR、main ブランチ、PVC の 3 層">
      <title>状態の置き場所 — 状態 / 定義 / 生ログの 3 層</title>
      {STATE_LAYERS.map((layer, i) => (
        <g key={layer.name}>
          <rect x={25} y={15 + i * 80} width={850} height={56} rx={2} className={`arch-node arch-node--${layer.tone}`} />
          <text x={41} y={49 + i * 80} className="arch-title">{layer.name}</text>
          <line x1={300} y1={25 + i * 80} x2={300} y2={61 + i * 80} className="arch-divider" />
          <text x={320} y={49 + i * 80} className="arch-sub">{layer.desc}</text>
        </g>
      ))}
    </svg>
  );
}

function TouchDiagram() {
  return (
    <svg className="arch-svg" viewBox="0 0 900 300" role="img" aria-label="人間との接点: Discord への push、Mission Control への pull、issue #56 の拒否権">
      <title>人間との接点 — push 型の Discord / pull 型のダッシュボード / issue #56 の拒否権</title>
      <defs>
        <marker id="arr-touch-push" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-head arch-head--signal" />
        </marker>
        <marker id="arr-touch-pull" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-head arch-head--blue" />
        </marker>
        <marker id="arr-touch-veto" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" className="arch-head arch-head--amber" />
        </marker>
      </defs>
      <rect x={30} y={45} width={180} height={230} rx={2} className="arch-node arch-node--accent" />
      <text x={120} y={140} textAnchor="middle" className="arch-title">autopilot</text>
      <text x={120} y={166} textAnchor="middle" className="arch-sub">heart + プロジェクト群</text>
      <rect x={690} y={45} width={180} height={230} rx={2} className="arch-node arch-node--amber" />
      <text x={780} y={140} textAnchor="middle" className="arch-title">人間</text>
      <text x={780} y={166} textAnchor="middle" className="arch-sub">介入は feedback と veto</text>
      <line x1={212} y1={85} x2={688} y2={85} className="arch-edge--signal" markerEnd="url(#arr-touch-push)" />
      <text x={450} y={73} textAnchor="middle" className="arch-lane-label">Discord webhook — push 型</text>
      <text x={450} y={105} textAnchor="middle" className="arch-sub">予告・納品・question・incident</text>
      <line x1={212} y1={160} x2={688} y2={160} className="arch-edge--blue" markerStart="url(#arr-touch-pull)" markerEnd="url(#arr-touch-pull)" />
      <text x={450} y={148} textAnchor="middle" className="arch-lane-label">Mission Control（この画面）— pull 型</text>
      <text x={450} y={180} textAnchor="middle" className="arch-sub">一覧・transcript・書き置き</text>
      <line x1={688} y1={235} x2={212} y2={235} className="arch-edge--amber" markerEnd="url(#arr-touch-veto)" />
      <text x={450} y={223} textAnchor="middle" className="arch-lane-label">issue #56 — 拒否権とフィードバック</text>
      <text x={450} y={255} textAnchor="middle" className="arch-sub">veto P-NNNN コメントで停止・次回起動で読まれる</text>
    </svg>
  );
}

const BLOCKS = [
  {
    id: "beat",
    num: "01",
    heading: "heart — 決定論の心臓",
    body: "ops/heart/ の常駐ループ。120 秒ごとにクラスタの実際を読み、状態機械を前進させ、必要な Job だけ起こす。LLM を呼ばないので、止まらないのが仕事。",
    diagram: <BeatDiagram />,
  },
  {
    id: "life",
    num: "02",
    heading: "プロジェクトの一生",
    body: "curriculum が VISION との差分から立案し、採択ゲートを通った案だけが予告を経て runner に託される。品質は独立レビューと受入 verify の実測が守る。",
    diagram: <LifeDiagram />,
  },
  {
    id: "state",
    num: "03",
    heading: "状態の置き場所",
    body: "プロジェクトの正はクラスタの Project CR で、heart だけが書ける。homelab 本体への変更は Git → CI → ArgoCD を通るものしかない。生の記録は PVC に落ちる。",
    diagram: <StateLayers />,
  },
  {
    id: "touch",
    num: "04",
    heading: "人間との接点",
    body: "Discord が押し込み、この画面が引き出し。判断は人間に投げず、人間が持つのはフィードバックと拒否権。issue #56 の veto は走行中でも有効。",
    diagram: <TouchDiagram />,
  },
];

export default function ArchitecturePage() {
  return (
    <main className="arch">
      <header className="arch__head">
        <a className="arch__back" href="/">← MISSION CONTROL</a>
        <p className="arch__eyebrow">HOW THIS HOUSE RUNS</p>
        <h1>autopilot 構成図</h1>
        <p className="arch__lede">
          homelab を人間の介入なしに保守し続ける器「heart-and-projects」の全体像。
          新参者が 5 分で全体を掴めることを基準に描いている。正典は ops/VISION.md。
        </p>
      </header>
      {BLOCKS.map((block) => (
        <section className="arch-block" key={block.id} aria-labelledby={`arch-${block.id}`}>
          <div className="arch-block__head">
            <span className="arch-block__num">{block.num}</span>
            <h2 id={`arch-${block.id}`}>{block.heading}</h2>
          </div>
          <p>{block.body}</p>
          <figure className="arch-figure">{block.diagram}</figure>
        </section>
      ))}
    </main>
  );
}

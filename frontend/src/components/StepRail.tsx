/**
 * 智能体执行流程图组件（OAG 版）
 * 版式、配色、交互与 RAG 版完全一致，只把节点拓扑换成 OAG 的实际链路：
 *   本体解析 →（未登记则拒绝）→ 组装上下文 → 生成 SQL → 规则校验 →（违反则修正并回环）→ 执行 SQL
 */
import { Check, Circle, LoaderCircle, X } from "lucide-react";
import { cn } from "../lib/format";
import type { ProgressStatus, StepState } from "../types/agent";

type FlowStatus = ProgressStatus | "pending";

type FlowNode = {
  step: string;
  x: number;
  y: number;
  w?: number;
};

const nodes: FlowNode[] = [
  { step: "本体解析", x: 410, y: 20, w: 176 },
  { step: "拒绝作答", x: 150, y: 112 },
  { step: "组装上下文", x: 410, y: 112, w: 176 },
  { step: "生成SQL", x: 410, y: 204 },
  { step: "规则校验", x: 410, y: 296 },
  { step: "修正SQL", x: 660, y: 296 },
  { step: "执行SQL", x: 410, y: 388 },
];

const connectors = [
  // 本体解析 --> 拒绝作答（未登记概念，直接结束）
  "M410 60 L410 84 L150 84 L150 106",
  // 本体解析 --> 组装上下文
  "M410 60 L410 106",
  // 组装上下文 --> 生成SQL
  "M410 152 L410 198",
  // 生成SQL --> 规则校验
  "M410 244 L410 290",
  // 规则校验 --> 修正SQL（违反规则）
  "M488 316 L582 316",
  // 修正SQL --> 执行SQL（只修一次，修完直接执行）
  "M660 336 L660 366 L486 366 L486 382",
  // 规则校验 --> 执行SQL（一次通过）
  "M410 336 L410 382",
];

const branchLabels = [
  { text: "未登记", x: 254, y: 76 },
  { text: "违反规则", x: 500, y: 308 },
  { text: "通过", x: 330, y: 366 },
];

function getStatusMap(steps: StepState[]) {
  return steps.reduce<Record<string, StepState>>((map, item) => {
    map[item.step] = item;
    return map;
  }, {});
}

function statusFor(step: string, map: Record<string, StepState>): FlowStatus {
  return map[step]?.status ?? "pending";
}

function NodeIcon({ status }: { status: FlowStatus }) {
  if (status === "running") {
    return <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />;
  }

  if (status === "success") {
    return <Check className="h-3.5 w-3.5" aria-hidden="true" />;
  }

  if (status === "error") {
    return <X className="h-3.5 w-3.5" aria-hidden="true" />;
  }

  return <Circle className="h-3.5 w-3.5" aria-hidden="true" />;
}

function FlowNodeCard({ node, status }: { node: FlowNode; status: FlowStatus }) {
  const width = node.w ?? 156;

  return (
    <div
      className="absolute -translate-x-1/2"
      style={{ left: node.x, top: node.y, width }}
    >
      <div
        className={cn(
          "flex h-10 items-center gap-2 border px-3 text-sm font-semibold shadow-line transition",
          status === "pending" && "border-ink/10 bg-white/55 text-ink/45",
          status === "running" && "border-brass/45 bg-brass/15 text-ink",
          status === "success" && "border-moss/25 bg-moss/10 text-ink",
          status === "error" && "border-tomato/35 bg-tomato/10 text-tomato",
        )}
      >
        <span
          className={cn(
            "grid h-6 w-6 shrink-0 place-items-center rounded-full",
            status === "pending" && "bg-ink/5 text-ink/35",
            status === "running" && "bg-brass/20 text-brass",
            status === "success" && "bg-moss/15 text-moss",
            status === "error" && "bg-tomato/15 text-tomato",
          )}
        >
          <NodeIcon status={status} />
        </span>
        <span className="min-w-0 flex-1 truncate">{node.step}</span>
      </div>
    </div>
  );
}

export function StepRail({ steps = [] }: { steps?: StepState[] }) {
  if (steps.length === 0) return null;

  const statusMap = getStatusMap(steps);

  return (
    <section className="mt-4 border border-ink/10 bg-white/40 px-3 py-4 shadow-line">
      <div className="mb-3 flex items-center justify-between gap-3 px-1">
        <div className="text-sm font-semibold text-ink">执行流程</div>
        <div className="text-xs text-ink/45">OAG · 本体驱动</div>
      </div>

      <div className="overflow-x-auto">
        <div className="relative mx-auto h-[460px] w-[820px]">
          <svg
            className="pointer-events-none absolute inset-0 h-full w-full"
            viewBox="0 0 820 460"
            fill="none"
            aria-hidden="true"
          >
            <defs>
              <marker
                id="flow-arrow"
                markerHeight="8"
                markerWidth="8"
                orient="auto"
                refX="6"
                refY="4"
              >
                <path d="M0 0 L8 4 L0 8 Z" fill="rgba(32,32,29,0.58)" />
              </marker>
            </defs>
            {connectors.map((path) => (
              <path
                key={path}
                d={path}
                stroke="rgba(32,32,29,0.5)"
                strokeWidth="1.5"
                markerEnd="url(#flow-arrow)"
              />
            ))}
            {branchLabels.map((label) => (
              <text
                key={label.text}
                x={label.x}
                y={label.y}
                fill="rgba(32,32,29,0.62)"
                fontSize="13"
                fontWeight="600"
              >
                {label.text}
              </text>
            ))}
          </svg>

          {nodes.map((node) => (
            <FlowNodeCard
              key={node.step}
              node={node}
              status={statusFor(node.step, statusMap)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

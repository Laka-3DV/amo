import type { Engine } from "../protocol";

import { agentNames } from "../agent-identity";
import dshIcon from "../assets/agents/dsh.svg?no-inline";
import codexIcon from "../assets/agents/codex.svg?no-inline";

/** Static provider marks identify the agent; solid dots remain execution state. */
export function AgentBadge({ engine = "dsh", className = "" }: { engine?: Engine | null; className?: string }) {
  const kind = engine ?? "dsh";
  return <span className={`agent-badge agent-${kind} ${className}`} aria-label={`Agent：${agentNames[kind]}`}>
    {kind === "codex" ? <span className="agent-logo agent-logo-mask" aria-hidden="true"
      style={{maskImage:`url("${codexIcon}")`,WebkitMaskImage:`url("${codexIcon}")`}} />
      : kind === "dsh" && <img className="agent-logo" src={dshIcon} alt="" aria-hidden="true" width="16" height="16" />}
    <span>{agentNames[kind]}</span>
  </span>;
}

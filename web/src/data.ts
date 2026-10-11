// Slash commands, models, permission modes for the Agent Remote client. DSH owns
// its slash commands, plugins and Agent composition; cc-remote only intercepts
// controls it can faithfully represent, every other slash is sent verbatim.

import type {
  CatalogModel, DshPresetInfo, EngineCapabilityItem, PermissionModeInfo,
} from "./protocol";

export interface CmdGroup { g: string }
export interface Cmd { slash: string; name: string; ds: string; ic: string }
export type Command = CmdGroup | Cmd;

// DSH owns slash commands, plugins and Agent composition. cc-remote only
// intercepts controls it can faithfully represent; every other slash is sent
// verbatim to DSH (for example its native /compact command).
export const DSH_COMMANDS: Command[] = [
  { g: "设置" },
  { slash: "model", name: "切换模型", ds: "选择 DSH Provider、模型与思考强度", ic: "cpu" },
  { g: "扩展" },
  { slash: "extensions", name: "Agent 能力", ds: "只读查看当前 Agent Preset 组合的 Skills", ic: "spark" },
  { slash: "skills", name: "Skills", ds: "只读查看当前 DSH Skills", ic: "read" },
  { g: "会话" },
  { slash: "preview", name: "预览文件", ds: "/preview <路径> 打开 Markdown 或 UTF-8 源文件", ic: "read" },
  { slash: "clear", name: "新会话", ds: "开始新的 DeepSeek Harness 会话", ic: "close" },
];

/** Present a non-default DSH Agent without leaking its transport identifier.
 *
 * The page title already says Code, so the built-in `code` preset would read
 * as the meaningless `Code · code`. Wait for catalog metadata, suppress the
 * default, and use only the catalog's display-safe friendly name.
 */
export const dshHeaderPresetLabel = (
  selectedId: string | null | undefined,
  presets: readonly DshPresetInfo[],
  defaultId: string | null | undefined,
): string | null => {
  if (!selectedId) return null;
  const preset = presets.find((candidate) => candidate.id === selectedId);
  if (!preset || preset.is_default || selectedId === defaultId) return null;
  return preset.name;
};

// Reasoning effort (思考强度). `name` is the RAW level id on purpose: it's what
// the engine's own configuration takes, so what the chip shows is exactly what
// you can grep for in the config. No translated labels.
export interface Effort { id: string; name: string; ds: string; ic: string }

// Blurb + icon per level, low -> high. Order here IS the cost/latency order, and is
// what we rank by — never trust arrival order from the server.
const EFFORT_META: Record<string, { ds: string; ic: string }> = {
  minimal: { ds: "几乎不推理 · 最快最省", ic: "gauge1" },
  low: { ds: "更快 · 轻推理", ic: "gauge1" },
  medium: { ds: "均衡 · 日常任务", ic: "gauge2" },
  high: { ds: "深度推理 · 复杂问题", ic: "gauge3" },
  xhigh: { ds: "更深推理 · 更慢", ic: "gauge4" },
  max: { ds: "最深推理 · 最难的问题", ic: "gauge5" },
  ultra: { ds: "极限推理 · 最慢最贵", ic: "crown" },
};
export const EFFORT_ORDER = Object.keys(EFFORT_META);
const rank = (id: string) => { const i = EFFORT_ORDER.indexOf(id); return i < 0 ? EFFORT_ORDER.length : i; };
const effort = (id: string): Effort => ({ id, name: id, ds: EFFORT_META[id]?.ds ?? "", ic: EFFORT_META[id]?.ic ?? "gauge3" });

export interface Model { id: string; name: string; ds: string; ic: string; efforts?: Effort[] }

/** Live catalogs by engine, as reported by the wrapper (`models` frame). */
export type Catalog = Record<string, CatalogModel[]>;

const fromCatalog = (entries: CatalogModel[]): Model[] =>
  entries.map((e) => ({
    id: e.id,
    name: e.display_name ?? e.id,
    ds: e.description ?? "",
    ic: "cpu",
    // The engine's empty list is authoritative: this model accepts no
    // reasoning override. Preserve [] instead of falling back to a generic
    // list which can fail only after a turn reaches the model API.
    efforts: e.efforts.map(effort),
  }));

export const modelsFor = (engine?: string, catalog?: Catalog): Model[] => {
  if (engine === "dsh" || engine === "codex") return fromCatalog(catalog?.[engine] ?? []);
  return [];
};

/** Model a NEW chat starts on: the engine's configured default, else the first
 * catalog entry. An EXISTING session's model is per-session and comes from the
 * wrapper, never from here. */
export const defaultModelFor = (engine?: string, catalog?: Catalog,
                                defaults?: Record<string, string>): string => {
  const list = modelsFor(engine, catalog);
  const want = engine ? defaults?.[engine] : undefined;
  return want && list.some((m) => m.id === want)
    ? want : list[0]?.id ?? "";
};

/** Effort levels the SELECTED model actually accepts. Unknown/unset model falls
 * back to the engine's baseline list. */
export const effortsFor = (engine?: string, model?: string | null, catalog?: Catalog): Effort[] => {
  const m = model ? modelsFor(engine, catalog).find((x) => x.id === model) : undefined;
  if (m?.efforts) return m.efforts;
  return [];
};

/** A null engine effort is a real model-default state, not loading. Ordinary
 * effort labels intentionally remain their raw CLI/config ids. */
export function effortNameForDisplay(
  id: string | null | undefined,
): string | null {
  const normalized = id?.trim();
  return normalized
    ? (normalized === "model-default" ? "模型默认" : normalized)
    : null;
}

export function effortIsSelectable(
  id: string | null | undefined,
): id is string {
  const normalized = id?.trim();
  return !!normalized && normalized !== "model-default";
}

/** Default effort = the HIGHEST level the selected model supports (product
 * decision: always think as hard as the model allows). Also what we clamp to
 * when the user switches to a model that lacks the current level. */
export const defaultEffortFor = (engine?: string, model?: string | null, catalog?: Catalog): string => {
  const list = effortsFor(engine, model, catalog);
  return list.reduce<Effort | null>(
    (current, candidate) => !current || rank(candidate.id) > rank(current.id)
      ? candidate : current,
    null,
  )?.id ?? "";
};

export interface Perm {
  id: string;
  name: string;
  short?: string;
  ds?: string;
  ic?: string;
  danger?: boolean;
}

export const permsFor = (
  engine?: string,
  dynamic?: PermissionModeInfo[] | null,
): Perm[] => (
  (engine === "dsh" || engine === "codex") ? (dynamic ?? []).map((option) => ({
    id: option.id,
    name: option.name,
    short: option.name,
    ds: option.description ?? undefined,
    ic: option.danger ? "bolt" : "shield",
    danger: option.danger,
  }))
    : []
);

/** Display an opaque host-owned permission state which is not selectable.
 * DSH's derived `custom` state is intentionally omitted from its command
 * options, but it is still a real current value and must not look like loading.
 */
export function permissionNameForDisplay(
  id: string | null | undefined,
): string | null {
  const normalized = id?.trim();
  if (!normalized) return null;
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(normalized)) return normalized;
  return normalized.split("-").map((word) =>
    word.slice(0, 1).toUpperCase() + word.slice(1)).join(" ");
}

// Map a reported model id to a catalog entry id. An id we don't know passes
// through verbatim.
export function matchModelId(m: string, engine?: string): string {
  const models = modelsFor(engine);
  const exact = models.find((x) => m === x.id);
  if (exact) return exact.id;
  return m;
}

export function isCmd(c: Command): c is Cmd {
  return (c as Cmd).slash !== undefined;
}

const DSH_CMD_LIST: Cmd[] = DSH_COMMANDS.filter(isCmd) as Cmd[];
export const DSH_CLIENT_SLASHES = new Set([
  "model", "extensions", "skills", "preview", "clear",
]);
export const commandsFor = (engine?: string, _surface: "code" | "work" = "code"): Command[] => (
  engine === "dsh" ? DSH_COMMANDS : []
);
export const clientSlashesFor = (engine?: string): Set<string> => (
  engine === "dsh" ? DSH_CLIENT_SLASHES : new Set()
);

// The command "token" the user is typing after "/", up to the first space.
// null when the input isn't an in-progress slash command (no leading "/", or a
// space already started the arguments). Drives the palette's show/hide.
export function slashToken(input: string): string | null {
  if (!input.startsWith("/")) return null;
  const after = input.slice(1);
  if (/\s/.test(after)) return null; // a space => choosing args, not the command
  return after;
}

/** Enabled Skills whose trigger starts with `token`, deduplicated by trigger. */
export function matchSkills(
  token: string,
  items: EngineCapabilityItem[],
): EngineCapabilityItem[] {
  const prefix = token.toLowerCase();
  const seen = new Set<string>();
  return items.filter((item) => {
    if (item.kind !== "skill" || item.enabled === false) return false;
    const name = item.name.toLowerCase();
    if (!name.startsWith(prefix) || seen.has(name)) return false;
    seen.add(name);
    return true;
  }).sort((a, b) => a.name.localeCompare(b.name));
}

// Commands whose slash starts with `token` (case-insensitive, prefix match).
export function matchCommands(token: string, engine?: string): Cmd[] {
  const t = token.toLowerCase();
  const list = engine === "dsh" ? DSH_CMD_LIST : [];
  return list.filter((c) => c.slash.toLowerCase().startsWith(t));
}

// Split "/slash rest of args" -> { slash, args }. null if not a slash line.
export function parseSlash(input: string): { slash: string; args: string } | null {
  if (!input.startsWith("/")) return null;
  const m = input.slice(1).match(/^(\S+)\s*([\s\S]*)$/);
  if (!m) return null;
  return { slash: m[1].toLowerCase(), args: m[2].trim() };
}

import {
  isCmd, commandsFor, modelsFor, effortsFor, effortIsSelectable, permsFor,
  type Cmd, type CmdGroup, type Catalog,
} from "../data";
import { Icon } from "../icons";
import { PickerSheet } from "./PickerSheet";
import type {
  Engine, PermissionModeInfo,
} from "../protocol";

interface Props {
  open: boolean;
  kind: "commands" | "models" | "efforts" | "perms";
  engine?: Engine;
  catalog?: Catalog;   // engine-reported models/efforts; falls back to data.ts
  // command mode: the token typed after "/" in the composer (prefix filter).
  // There is NO input box in this sheet anymore — the composer textarea is the
  // single input, and this palette is a live suggestion overlay driven by it.
  filter?: string;
  onClose: () => void;
  onPickCommand?: (slash: string) => void;
  currentModel?: string;
  onPickModel?: (model: string) => void;
  currentEffort?: string;
  onPickEffort?: (effort: string) => void;
  currentPerm?: string;
  permOptions?: PermissionModeInfo[] | null;
  onPickPerm?: (perm: string) => void;
}

export function CommandSheet({
  open, kind, engine, catalog, filter = "", onClose, onPickCommand,
  currentModel, onPickModel, currentEffort, onPickEffort, currentPerm,
  permOptions, onPickPerm,
}: Props) {
  // A translated, closed fixed panel can be brought back into view by iOS
  // keyboard insets. Closed pickers must have no painted or focusable surface.
  if (!open) return null;
  const isCmdMode = kind === "commands";
  const isPermMode = kind === "perms";
  const isEffortMode = kind === "efforts";
  const f = filter.toLowerCase();
  // Effort levels are per-model, so the effort sheet must be built from the model
  // currently selected, not just the engine.
  const MODELS = modelsFor(engine, catalog);
  const EFFORTS = effortsFor(engine, currentModel, catalog);
  const PERMS = permsFor(engine, permOptions);

  // Prefix-match on the slash (same rule the composer uses to decide visibility),
  // preserving group headers that still have matches.
  const groups: { name: string; cmds: Cmd[] }[] = [];
  if (isCmdMode) {
    for (const c of commandsFor(engine)) {
      if (isCmd(c)) {
        if (!f || c.slash.toLowerCase().startsWith(f)) {
          if (!groups.length) groups.push({ name: "", cmds: [] });
          groups[groups.length - 1].cmds.push(c);
        }
      } else {
        groups.push({ name: (c as CmdGroup).g, cmds: [] });
      }
    }
  }
  const visible = groups.filter((g) => g.cmds.length > 0);

  const title = isCmdMode ? "命令面板"
    : isPermMode ? "选择权限模式"
    : isEffortMode ? "选择思考强度" : "选择模型";

  return (
    <PickerSheet open={open} title={title} onClose={onClose}
      heading={isCmdMode && filter ? `/${filter}` : title}
      className={isPermMode ? "permission-sheet" : ""}>
      {isCmdMode ? (
        visible.map((g, gi) => (
          <div key={gi}>
            <div className="cmd-group">{g.name}</div>
            {g.cmds.map((c) => (
              <button key={c.slash} className="cmd" onClick={() => onPickCommand?.(c.slash)}>
                <span className="cmd-ic"><Icon name={c.ic} size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm"><span className="slash">/{c.slash}</span></span>
                  <span className="cmd-ds">{c.name} — {c.ds}</span>
                </span>
                <span className="cmd-kbd">↵</span>
              </button>
            ))}
          </div>
        ))
      ) : isPermMode ? (
        <>
          <div className="permission-options permission-approvals">
            {PERMS.map((p) => (
              <button
                key={p.id}
                className={"cmd" + (p.id === currentPerm ? " sel" : "") + (p.danger ? " danger" : "")}
                onClick={() => onPickPerm?.(p.id)}
              >
                <span className="cmd-ic"><Icon name={p.ic ?? (p.danger ? "bolt" : "shield")} size={17} /></span>
                <span className="cmd-tx">
                  <span className="cmd-nm">{p.name}</span>
                  <span className="cmd-ds">{p.ds}</span>
                </span>
                {p.id === currentPerm
                  ? <span className="cmd-check"><Icon name="check" size={19} /></span>
                  : <span className="cmd-kbd" />}
              </button>
            ))}
          </div>
        </>
      ) : isEffortMode ? (
        EFFORTS.map((ef) => (
          <button
            key={ef.id}
            className={"cmd" + (
              effortIsSelectable(currentEffort) && ef.id === currentEffort
                ? " sel" : "")}
            onClick={() => onPickEffort?.(ef.id)}
          >
            <span className="cmd-ic"><Icon name={ef.ic} size={17} /></span>
            <span className="cmd-tx">
              <span className="cmd-nm">{ef.name}</span>
              <span className="cmd-ds">{ef.ds}</span>
            </span>
            {effortIsSelectable(currentEffort) && ef.id === currentEffort
              ? <span className="cmd-check"><Icon name="check" size={19} /></span>
              : <span className="cmd-kbd" />}
          </button>
        ))
      ) : (
        MODELS.map((m) => (
          <button
            key={m.id}
            className={"cmd" + (m.id === currentModel ? " sel" : "")}
            onClick={() => onPickModel?.(m.id)}
          >
            <span className="cmd-ic"><Icon name={m.ic} size={17} /></span>
            <span className="cmd-tx">
              <span className="cmd-nm">{m.name}</span>
              <span className="cmd-ds">{m.ds}</span>
            </span>
            {m.id === currentModel
              ? <span className="cmd-check"><Icon name="check" size={19} /></span>
              : <span className="cmd-kbd" />}
          </button>
        ))
      )}
    </PickerSheet>
  );
}

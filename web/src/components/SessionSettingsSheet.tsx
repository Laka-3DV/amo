import { Icon } from "../icons";
import { PickerSheet } from "./PickerSheet";

interface SettingRow {
  label: string;
  value: string;
  actionLabel: string;
  icon: string;
  onClick?: () => void;
  disabled?: boolean;
}

/** Read-only until a field is deliberately opened and a real host option picked. */
export function SessionSettingsSheet({ open, onClose, rows, note }: {
  open: boolean;
  onClose: () => void;
  rows: SettingRow[];
  note?: string;
}) {
  return <PickerSheet open={open} title="会话配置" onClose={onClose} className="session-settings-sheet">
    <div className="session-settings-rows">
      {rows.map(row => <button key={row.label} type="button" className="session-setting"
        aria-label={row.actionLabel} disabled={row.disabled || !row.onClick} onClick={row.onClick}>
        <Icon name={row.icon} size={18} />
        <span><small>{row.label}</small><b>{row.value}</b></span>
        {row.onClick && <Icon name="chevron-right" size={16} />}
      </button>)}
    </div>
    {note && <p className="session-settings-note">{note}</p>}
  </PickerSheet>;
}

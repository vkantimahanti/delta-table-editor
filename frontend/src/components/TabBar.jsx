import React from 'react'
import {
  Table2, Upload, History, Download,
  Plus, Pencil, Trash2, Save,
  Clock, RotateCcw, X
} from 'lucide-react'
import styles from './TabBar.module.css'

const TABS = [
  { id: 'data',    label: 'Data edit', icon: Table2   },
  { id: 'upload',  label: 'Upload Data',   icon: Upload   },
  { id: 'export',  label: 'Export Data',   icon: Download },
  { id: 'history', label: 'History',       icon: History  },
]

export default function TabBar({
  active, onChange,
  canUpdate, canDelete, selectedCount, changedCount,
  onAdd, onUpdate, onDelete, onSave, onCancelChanges,
  deltaVersion, onDeltaVersion, deltaVersions,
}) {
  return (
    <div className={styles.wrap}>
      <div className={styles.tabRow}>
        {TABS.map(tab => {
          const Icon = tab.icon
          const isActive = active === tab.id
          return (
            <button
              key={tab.id}
              className={`${styles.tab} ${isActive ? styles.tabActive : ''}`}
              onClick={() => onChange(tab.id)}
            >
              <Icon size={14} />
              {tab.label}
            </button>
          )
        })}
      </div>

      <div className={styles.actionBar}>
        {active === 'data' && (
          <>
            <div className={styles.actionSpacer} />
            <button className={`${styles.actionBtn} ${styles.btnAdd}`} onClick={onAdd}>
              <Plus size={13} /> Add Row
            </button>
            <button
              className={`${styles.actionBtn} ${styles.btnUpdate}`}
              onClick={onUpdate}
              disabled={!canUpdate}
            >
              <Pencil size={13} /> Update
            </button>
            <button
              className={`${styles.actionBtn} ${styles.btnDelete}`}
              onClick={onDelete}
              disabled={!canDelete}
            >
              <Trash2 size={13} />
              Delete Selected{selectedCount > 0 ? ` (${selectedCount})` : ''}
            </button>

            <div className={styles.actionSpacer} />

            {changedCount > 0 && (
              <button className={`${styles.actionBtn} ${styles.btnCancel}`} onClick={onCancelChanges}>
                <X size={13} /> Cancel
              </button>
            )}
            <button
              className={`${styles.actionBtn} ${styles.btnSave}`}
              onClick={onSave}
              disabled={changedCount === 0}
            >
              <Save size={13} />
              Review &amp; Save {changedCount > 0 ? `(${changedCount})` : ''}
            </button>
          </>
        )}

        {active === 'upload' && (
          <span className={styles.tabContext}>Upload filtered or full table data via CSV</span>
        )}

        {active === 'export' && (
          <span className={styles.tabContext}>Server export of all rows matching current filters</span>
        )}

        {active === 'history' && (
          <>
            <span className={styles.tabContext}>
              <History size={13} />
              Change history
            </span>
            <div className={styles.actionDivider} />
            <label className={styles.actionLabel}>
              <Clock size={12} /> Delta version
            </label>
            <select
              className={styles.actionSelect}
              value={deltaVersion}
              onChange={e => onDeltaVersion(e.target.value)}
              aria-label="Delta version"
            >
              <option value="latest">Latest (current)</option>
              {(deltaVersions || []).map(v => (
                <option key={v.version} value={v.version}>
                  v{v.version} — {v.timestamp}
                </option>
              ))}
            </select>
            <button className={`${styles.actionBtn} ${styles.btnUpdate}`} style={{ marginLeft: 6 }}>
              <RotateCcw size={13} /> View snapshot
            </button>
          </>
        )}
      </div>
    </div>
  )
}

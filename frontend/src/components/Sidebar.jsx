import React from 'react'
import {
  LayoutDashboard, Table2, ShieldCheck,
  ChevronLeft, ChevronRight, Database,
} from 'lucide-react'
import styles from './Sidebar.module.css'

const NAV = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'workspace', label: 'Data Editor', icon: Table2 },
  { id: 'approvals', label: 'Approvals', icon: ShieldCheck, badgeKey: 'approvals' },
]

export default function Sidebar({
  activeSection,
  onNavigate,
  collapsed,
  onToggleCollapse,
  pendingApprovalCount = 0,
}) {
  return (
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ''}`}>
      <div className={styles.brand}>
        <div className={styles.brandIcon}>
          <Database size={20} />
        </div>
        {!collapsed && (
          <div>
            <div className={styles.brandTitle}>Delta Editor</div>
            <div className={styles.brandSub}>Databricks Apps</div>
          </div>
        )}
      </div>

      <nav className={styles.nav}>
        {NAV.map(({ id, label, icon: Icon, disabled, badgeKey }) => {
          const badge = badgeKey === 'approvals' && pendingApprovalCount > 0
            ? pendingApprovalCount
            : null
          return (
            <button
              key={id}
              type="button"
              className={`${styles.navItem} ${activeSection === id ? styles.navItemActive : ''}`}
              onClick={() => !disabled && onNavigate(id)}
              disabled={disabled}
              title={collapsed ? label : undefined}
            >
              <Icon size={18} />
              {!collapsed && <span>{label}</span>}
              {!collapsed && badge != null && (
                <span className={styles.badge}>{badge}</span>
              )}
              {!collapsed && disabled && <span className={styles.soon}>Soon</span>}
            </button>
          )
        })}
      </nav>

      <div className={styles.footer}>
        <button
          type="button"
          className={styles.collapseBtn}
          onClick={onToggleCollapse}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  )
}

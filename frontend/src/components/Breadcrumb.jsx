import React from 'react'
import { ChevronRight } from 'lucide-react'
import styles from './Breadcrumb.module.css'

const TAB_LABELS = {
  data: 'Data edit',
  upload: 'Upload Data',
  export: 'Export Data',
  history: 'History',
}

const SECTION_LABELS = {
  overview: 'Overview',
  workspace: 'Data Editor',
  approvals: 'Approvals',
}

export function buildBreadcrumbItems({
  activeSection,
  selectedGroup,
  catalog,
  schema,
  table,
  tableLabel,
  activeTab,
}) {
  const items = [{ label: 'Delta Table Editor', key: 'app' }]

  if (activeSection === 'overview') {
    items.push({ label: SECTION_LABELS.overview, key: 'overview' })
    return items
  }

  if (activeSection === 'approvals') {
    items.push({ label: SECTION_LABELS.approvals, key: 'approvals' })
    return items
  }

  if (activeSection === 'workspace') {
    items.push({ label: SECTION_LABELS.workspace, key: 'workspace' })
    if (selectedGroup && selectedGroup !== 'All') {
      items.push({ label: selectedGroup, key: 'group' })
    }
    if (catalog) items.push({ label: catalog, key: 'catalog' })
    if (schema) items.push({ label: schema, key: 'schema' })
    if (table) {
      items.push({ label: tableLabel || table, key: 'table' })
    }
    if (activeTab && TAB_LABELS[activeTab]) {
      items.push({ label: TAB_LABELS[activeTab], key: 'tab' })
    }
  }

  return items
}

export default function Breadcrumb({ items }) {
  if (!items?.length) return null

  return (
    <nav className={styles.bar} aria-label="Breadcrumb">
      <ol className={styles.list}>
        {items.map((item, index) => {
          const isLast = index === items.length - 1
          return (
            <li key={item.key || `${item.label}-${index}`} className={styles.item}>
              {index > 0 && (
                <ChevronRight size={14} className={styles.sep} aria-hidden="true" />
              )}
              <span
                className={`${styles.crumb} ${isLast ? styles.crumbActive : ''}`}
                aria-current={isLast ? 'page' : undefined}
                title={item.label}
              >
                {item.label}
              </span>
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

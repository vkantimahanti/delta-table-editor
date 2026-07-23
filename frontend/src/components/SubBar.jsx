import React from 'react'
import { Table2, Key, List, Filter, X } from 'lucide-react'
import styles from './SubBar.module.css'

export default function SubBar({
  schema, table, pkCols, rowCount,
  totalColumns = 0, visibleColumnCount = 0,
  activeFilters, onRemoveFilter,
}) {
  if (!table) return null
  return (
    <div className={styles.bar}>
      <div className={styles.left}>
        <span className={styles.pill}>
          <Table2 size={12} />
          <strong>{schema}.{table}</strong>
        </span>
        {pkCols.length > 0 && (
          <span className={styles.pill}>
            <Key size={12} />
            {pkCols.join(', ')}
          </span>
        )}
        <span className={styles.pill}>
          <List size={12} />
          <strong>{rowCount}</strong>&nbsp;rows
        </span>
        {totalColumns > 0 && (
          <span className={styles.pill}>
            <strong>{visibleColumnCount}</strong>
            {visibleColumnCount === totalColumns
              ? ` column${totalColumns === 1 ? '' : 's'}`
              : ` of ${totalColumns} columns`}
          </span>
        )}
        {activeFilters.map(f => (
          <span key={f.column} className={styles.filterChip}>
            <Filter size={10} />
            {f.column}{f.value ? `="${f.value}"` : ' is empty'}
            <button onClick={() => onRemoveFilter(f.column)} aria-label={`Remove filter on ${f.column}`}>
              <X size={11} />
            </button>
          </span>
        ))}
      </div>
    </div>
  )
}

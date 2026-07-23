import React from 'react'
import { RefreshCw, Filter, X } from 'lucide-react'
import NavSelect from './NavSelect.jsx'
import styles from './WorkspaceHeader.module.css'

export default function WorkspaceHeader({
  catalogs, schemas, tables,
  groups, selectedGroup, onGroupChange,
  catalog, schema, table,
  loading, onCatalog, onSchema, onTable, onRefresh,
  totalCount, totalColumns, activeFilters, onRemoveFilter,
}) {
  return (
    <header className={styles.header}>
      <div className={styles.titleRow}>
        <div className={styles.titleBlock}>
          <h1 className={styles.title}>Data Editor</h1>
          {!table && (
            <p className={styles.subtitle}>
              View, edit, upload or export data from your tables.
            </p>
          )}
        </div>

        <div className={styles.titleActions}>
          {table && (
            <div className={styles.metaPills} aria-label="Table summary">
              {totalCount != null && (
                <span className={styles.metaPill}>
                  <strong>{totalCount.toLocaleString()}</strong> rows
                </span>
              )}
              {totalColumns > 0 && (
                <span className={styles.metaPill}>
                  <strong>{totalColumns}</strong> cols
                </span>
              )}
              <span className={styles.statusPill}>
                <span className={styles.statusDot} aria-hidden="true" />
                Active
              </span>
            </div>
          )}
          <button
            type="button"
            className={styles.refreshBtn}
            onClick={onRefresh}
            disabled={loading || !table}
          >
            <RefreshCw size={14} className={loading ? styles.spin : ''} />
            Refresh
          </button>
        </div>
      </div>

      <div className={styles.selectorRow}>
        <div className={styles.selectors}>
          {groups.length > 1 && (
            <label className={styles.field}>
              <span>Group</span>
              <NavSelect
                value={selectedGroup}
                onChange={onGroupChange}
                options={groups}
                disabled={loading}
                ariaLabel="Select app group"
              />
            </label>
          )}
          <label className={styles.field}>
            <span>Catalog</span>
            <NavSelect value={catalog} onChange={onCatalog} options={catalogs} disabled={loading} ariaLabel="Select catalog" />
          </label>
          <label className={styles.field}>
            <span>Schema</span>
            <NavSelect value={schema} onChange={onSchema} options={schemas} disabled={!catalog || loading} ariaLabel="Select schema" />
          </label>
          <label className={styles.field}>
            <span>Table</span>
            <NavSelect value={table} onChange={onTable} options={tables} disabled={!schema || loading} ariaLabel="Select table" />
          </label>
        </div>

        {table && activeFilters.length > 0 && (
          <div className={styles.filters}>
            {activeFilters.map(f => (
              <span key={f.column} className={styles.filterChip}>
                <Filter size={11} />
                {f.column}{f.value ? `="${f.value}"` : ' is empty'}
                <button type="button" onClick={() => onRemoveFilter(f.column)} aria-label={`Remove filter ${f.column}`}>
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
    </header>
  )
}

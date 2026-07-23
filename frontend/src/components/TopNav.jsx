import React from 'react'
import { Database, RefreshCw, Settings, Columns3, ChevronDown } from 'lucide-react'
import NavSelect from './NavSelect.jsx'
import styles from './TopNav.module.css'

export default function TopNav({
  catalogs, schemas, tables,
  groups, selectedGroup, onGroupChange,
  catalog, schema, table,
  allColumns, visibleColumns, onVisibleColumnsChange,
  loading,
  onCatalog, onSchema, onTable, onRefresh,
}) {
  const [colOpen, setColOpen] = React.useState(false)
  const [colSearch, setColSearch] = React.useState('')
  const colRef = React.useRef()
  const colSearchRef = React.useRef()

  React.useEffect(() => {
    function outside(e) {
      if (colRef.current && !colRef.current.contains(e.target)) closeColPicker()
    }
    document.addEventListener('mousedown', outside)
    return () => document.removeEventListener('mousedown', outside)
  }, [])

  React.useEffect(() => {
    if (colOpen) {
      setColSearch('')
      requestAnimationFrame(() => {
        colSearchRef.current?.focus()
        colSearchRef.current?.select()
      })
    }
  }, [colOpen])

  function closeColPicker() {
    setColOpen(false)
    setColSearch('')
  }

  const filteredColumns = allColumns.filter(col => {
    const q = colSearch.trim().toLowerCase()
    if (!q) return true
    const name = col.column_name.toLowerCase()
    const label = (col.display_label || col.column_name).toLowerCase()
    return name.includes(q) || label.includes(q)
  })

  function toggleCol(key) {
    if (visibleColumns.includes(key)) {
      if (visibleColumns.length <= 1) return
      onVisibleColumnsChange(visibleColumns.filter(k => k !== key))
    } else {
      const allKeys = allColumns.map(c => c.column_name)
      const ordered = allKeys.filter(k => visibleColumns.includes(k) || k === key)
      onVisibleColumnsChange(ordered)
    }
  }

  const colLabel = allColumns.length === 0
    ? 'Columns'
    : visibleColumns.length === allColumns.length
      ? `All ${allColumns.length} columns`
      : `${visibleColumns.length} of ${allColumns.length} columns`

  return (
    <nav className={styles.nav}>
      <div className={styles.brand}>
        <Database size={18} className={styles.brandIcon} />
        <span>Data Canvas</span>
      </div>

      {groups.length > 1 && (
        <>
          <div className={`${styles.navItem} ${styles.navItemGroup}`}>
            <span className={styles.navLabel}>GROUP</span>
            <NavSelect
              value={selectedGroup}
              onChange={onGroupChange}
              options={groups}
              disabled={loading}
              ariaLabel="Select app group"
            />
          </div>
          <span className={styles.sep}>›</span>
        </>
      )}

      <div className={styles.chain}>
        {/* Catalog */}
        <div className={`${styles.navItem} ${styles.navItemCatalog}`}>
          <span className={styles.navLabel}>CATALOG</span>
          <NavSelect
            value={catalog}
            onChange={onCatalog}
            options={catalogs}
            disabled={loading}
            ariaLabel="Select catalog"
          />
        </div>

        <span className={styles.sep}>›</span>

        {/* Schema */}
        <div className={`${styles.navItem} ${styles.navItemSchema}`}>
          <span className={styles.navLabel}>SCHEMA</span>
          <NavSelect
            value={schema}
            onChange={onSchema}
            options={schemas}
            disabled={!catalog || loading}
            ariaLabel="Select schema"
          />
        </div>

        <span className={styles.sep}>›</span>

        {/* Table */}
        <div className={`${styles.navItem} ${styles.navItemTable}`}>
          <span className={styles.navLabel}>TABLE</span>
          <NavSelect
            value={table}
            onChange={onTable}
            options={tables}
            disabled={!schema || loading}
            ariaLabel="Select table"
          />
        </div>

        {/* Columns picker */}
        {allColumns.length > 0 && (
          <>
            <span className={styles.sep}>›</span>
            <div className={`${styles.navItem} ${styles.navItemColumns}`} ref={colRef}>
              <span className={styles.navLabel}>COLUMNS</span>
              <div className={`${styles.navVal} ${colOpen ? styles.navValOpen : ''}`}>
                <div className={styles.navSelectBtn}>
                  {colOpen ? (
                    <input
                      ref={colSearchRef}
                      type="text"
                      className={styles.navSelectInput}
                      value={colSearch}
                      placeholder="Type to filter columns…"
                      aria-label="Filter columns"
                      onChange={e => setColSearch(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Escape') closeColPicker() }}
                    />
                  ) : (
                    <button
                      type="button"
                      className={styles.colPickerBtn}
                      onClick={() => setColOpen(true)}
                      aria-label="Choose columns"
                      aria-expanded={colOpen}
                    >
                      <Columns3 size={13} />
                      <span>{colLabel}</span>
                    </button>
                  )}
                  <button
                    type="button"
                    className={styles.navSelectCaretBtn}
                    aria-label="Column picker menu"
                    tabIndex={-1}
                    onMouseDown={e => e.preventDefault()}
                    onClick={() => (colOpen ? closeColPicker() : setColOpen(true))}
                  >
                    <ChevronDown size={13} className={`${styles.caret} ${colOpen ? styles.caretOpen : ''}`} />
                  </button>
                </div>

              {colOpen && (
                <div className={styles.colDropdown}>
                  <div className={styles.colDropHeader}>
                    <span>Display columns</span>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        className={styles.colDropLink}
                        onClick={() => onVisibleColumnsChange(allColumns.map(c => c.column_name))}
                      >All</button>
                      <button
                        className={styles.colDropLink}
                        onClick={() => {
                          const pks = allColumns.filter(c => c.is_pk).map(c => c.column_name)
                          onVisibleColumnsChange(pks.length ? pks : [allColumns[0].column_name])
                        }}
                      >PKs only</button>
                    </div>
                  </div>
                  <div className={styles.colList}>
                    {filteredColumns.map(col => (
                      <label key={col.column_name} className={styles.colItem}>
                        <input
                          type="checkbox"
                          checked={visibleColumns.includes(col.column_name)}
                          onChange={() => toggleCol(col.column_name)}
                        />
                        <span>{col.display_label || col.column_name}</span>
                        {col.is_pk && <span className={styles.colPkBadge}>PK</span>}
                      </label>
                    ))}
                    {colSearch.trim() && filteredColumns.length === 0 && (
                      <div className={styles.colSearchEmpty}>No matching columns</div>
                    )}
                  </div>
                </div>
              )}
              </div>
            </div>
          </>
        )}
      </div>

      <div className={styles.navActions}>
        <button
          className={styles.iconBtn}
          onClick={onRefresh}
          disabled={loading}
          title="Refresh data"
          aria-label="Refresh"
        >
          <RefreshCw size={15} className={loading ? styles.spin : ''} />
        </button>
        <button
          type="button"
          className={`${styles.iconBtn} ${styles.iconBtnPlaceholder}`}
          title="Settings (coming soon)"
          aria-label="Settings — coming soon"
          disabled
        >
          <Settings size={15} />
        </button>
      </div>
    </nav>
  )
}

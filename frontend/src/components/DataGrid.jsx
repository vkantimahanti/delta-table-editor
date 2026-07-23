import React from 'react'
import styles from './DataGrid.module.css'

export default function DataGrid({
  columns,           // visible ColumnMeta[]
  allColumns,        // all ColumnMeta[] (for editable check)
  rowItems,          // { row, sourceIdx }[] — sourceIdx indexes into full rows array
  editedRows,        // { [sourceIdx]: { col: val } }
  newRows,           // Set of source indices that are new
  selectedRows,      // Set of source indices
  colFilters,        // { col: value }
  dropdownCache,     // { col: string[] }
  dependentDropdowns, // { col: { parent_column, options_by_parent } }
  pkCols,
  allowUpdate,
  onCellChange,
  onToggleRow,
  onToggleAll,
  onColFilter,
}) {
  const allChecked = rowItems.length > 0 && rowItems.every(({ sourceIdx }) => selectedRows.has(sourceIdx))

  function lookupDependentOptions(dep, parentVal) {
    const raw = String(parentVal ?? '').trim()
    const map = dep?.options_by_parent || {}
    if (!raw) return []
    if (map[raw]) return map[raw]
    const key = Object.keys(map).find(
      k => String(k).trim().toLowerCase() === raw.toLowerCase()
    )
    return key ? map[key] : []
  }

  function cellDropdownOpts(colName, draft, currentVal) {
    let opts
    const dep = dependentDropdowns?.[colName]
    if (dep?.parent_column) {
      opts = lookupDependentOptions(dep, draft[dep.parent_column])
    } else {
      opts = [...(dropdownCache[colName] || [])]
    }

    // Keep existing cell value visible even if it uses legacy formatting
    // (e.g. "HIGHMARK    |    _ALL_") not present in master lookup options.
    const existing = currentVal == null ? '' : String(currentVal)
    if (existing && !opts.some(o => String(o) === existing)) {
      opts = [existing, ...opts]
    }
    return opts
  }

  function isDependentColumn(colName) {
    return !!dependentDropdowns?.[colName]?.parent_column
  }

  function isDropdownColumn(col) {
    return col.col_type === 'dropdown'
  }

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        <thead>
          {/* Column headers */}
          <tr className={styles.headMain}>
            <th className={styles.checkTh}>
              <input
                type="checkbox"
                checked={allChecked}
                onChange={e => onToggleAll(e.target.checked)}
                aria-label="Select all rows"
              />
            </th>
            {columns.map(col => (
              <th
                key={col.column_name}
                className={pkCols.includes(col.column_name) ? styles.pkTh : ''}
              >
                <span className={styles.colName}>{col.display_label || col.column_name}</span>
                <span className={styles.colType}>{col.col_type || 'string'}</span>
                {pkCols.includes(col.column_name) && (
                  <span className={styles.pkBadge} title="Primary key (config)">PK</span>
                )}
              </th>
            ))}
          </tr>

          {/* Inline column filters */}
          <tr className={styles.headFilter}>
            <th className={styles.checkTh} />
            {columns.map(col => {
              const opts = dropdownCache[col.column_name]
              return (
                <th key={col.column_name} className={styles.filterTh}>
                  {opts?.length > 0 ? (
                    <select
                      className={styles.filterSel}
                      value={colFilters[col.column_name] || ''}
                      onChange={e => onColFilter(col.column_name, e.target.value)}
                      aria-label={`Filter ${col.display_label || col.column_name}`}
                    >
                      <option value="">All</option>
                      {opts.map(o => <option key={o} value={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input
                      className={styles.filterInp}
                      placeholder={`Filter…`}
                      value={colFilters[col.column_name] || ''}
                      onChange={e => onColFilter(col.column_name, e.target.value)}
                      aria-label={`Filter ${col.display_label || col.column_name}`}
                    />
                  )}
                </th>
              )
            })}
          </tr>
        </thead>

        <tbody>
          {rowItems.length === 0 && (
            <tr>
              <td colSpan={columns.length + 1} className={styles.emptyRow}>
                No rows to display.
              </td>
            </tr>
          )}
          {rowItems.map(({ row, sourceIdx }) => {
            const isNew     = newRows.has(sourceIdx)
            const isChanged = !!editedRows[sourceIdx]
            const isSelected = selectedRows.has(sourceIdx)
            const draft = { ...row, ...(editedRows[sourceIdx] || {}) }

            return (
              <tr
                key={sourceIdx}
                className={[
                  isNew     ? styles.rowNew     : '',
                  isChanged ? styles.rowChanged : '',
                  isSelected ? styles.rowSel    : '',
                ].filter(Boolean).join(' ')}
              >
                <td className={styles.checkTd}>
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={e => onToggleRow(sourceIdx, e.target.checked)}
                    aria-label={`Select row ${sourceIdx + 1}`}
                  />
                </td>
                {columns.map(col => {
                  const isPk = pkCols.includes(col.column_name)
                  const editable = col.is_editable && allowUpdate && !isPk
                  const editablePk = isNew && isPk && col.is_editable
                  const autoPk = isNew && isPk && !col.is_editable
                  const val = draft[col.column_name] ?? ''
                  const opts = cellDropdownOpts(col.column_name, draft, val)

                  if (autoPk) {
                    return (
                      <td key={col.column_name}>
                        <div
                          className={`${styles.cellVal} ${styles.pkVal} ${styles.autoPk}`}
                          title="Assigned automatically when you save"
                        >
                          {String(val) || 'Auto on save'}
                        </div>
                      </td>
                    )
                  }

                  if (editable || editablePk) {
                    const dependent = isDependentColumn(col.column_name)
                    const hasVal = String(val).length > 0
                    const useSelect = isDropdownColumn(col) && (opts?.length > 0 || dependent)
                    if (useSelect) {
                      return (
                        <td key={col.column_name} className={styles.editTd}>
                          <select
                            className={`${styles.cellEdit} ${styles.cellSelect} ${editablePk ? styles.pkEdit : ''}`}
                            value={val}
                            onChange={e => onCellChange(sourceIdx, col.column_name, e.target.value)}
                            aria-label={col.display_label || col.column_name}
                            disabled={dependent && opts.length === 0 && !hasVal}
                            title={val || (dependent && opts.length === 0 && !hasVal ? 'Select Carrier first' : undefined)}
                          >
                            <option value="">
                              {dependent && !hasVal && opts.length === 0
                                ? 'Select Carrier first…'
                                : ''}
                            </option>
                            {opts.map(o => <option key={o} value={o} title={o}>{o}</option>)}
                          </select>
                        </td>
                      )
                    }
                    return (
                      <td key={col.column_name} className={styles.editTd}>
                        <input
                          type="text"
                          className={`${styles.cellEdit} ${editablePk ? styles.pkEdit : ''}`}
                          value={val}
                          onChange={e => onCellChange(sourceIdx, col.column_name, e.target.value)}
                          aria-label={col.display_label || col.column_name}
                          autoComplete="off"
                        />
                      </td>
                    )
                  }

                  return (
                    <td key={col.column_name}>
                      <div className={`${styles.cellVal} ${isPk ? styles.pkVal : ''}`}>
                        {String(val)}
                      </div>
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

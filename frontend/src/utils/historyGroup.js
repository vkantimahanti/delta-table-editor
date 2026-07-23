/** Format audit timestamp for display. */
export function formatHistoryTimestamp(changedAt) {
  return String(changedAt || '').replace('T', ' ').substring(0, 19)
}

/** Parse record_key JSON (or passthrough object) into a plain object. */
export function parseRecordKey(recordKey) {
  if (!recordKey) return {}
  if (typeof recordKey === 'object') return recordKey
  try {
    return JSON.parse(recordKey)
  } catch {
    return { key: String(recordKey) }
  }
}

function groupKey(entry) {
  return [
    entry.changed_at,
    entry.changed_by,
    entry.record_key,
    entry.change_source,
  ].join('\0')
}

function displayCell(value) {
  if (value === null || value === undefined || String(value).trim() === '') return '(empty)'
  return String(value)
}

/**
 * Group flat audit rows into comparison blocks (one record change batch each).
 * Each block contains column lists and before/after row values.
 */
export function groupHistoryForComparison(history) {
  const groups = new Map()

  for (const entry of history) {
    const key = groupKey(entry)
    if (!groups.has(key)) {
      groups.set(key, {
        changed_at: entry.changed_at,
        changed_by: entry.changed_by,
        record_key: entry.record_key,
        change_source: entry.change_source,
        changes: [],
      })
    }
    groups.get(key).changes.push(entry)
  }

  return Array.from(groups.values()).map(buildComparisonGroup)
}

function buildComparisonGroup(group) {
  const pk = parseRecordKey(group.record_key)
  const pkCols = Object.keys(pk)
  const pkLower = new Set(pkCols.map(c => c.toLowerCase()))
  const changedCols = []

  for (const change of group.changes) {
    const col = change.column_name
    if (!col) continue
    if (pkLower.has(col.toLowerCase())) continue
    if (changedCols.some(c => c.toLowerCase() === col.toLowerCase())) continue
    changedCols.push(col)
  }

  const columns = [...pkCols, ...changedCols]
  const previous = {}
  const current = {}

  for (const col of pkCols) {
    previous[col] = displayCell(pk[col])
    current[col] = displayCell(pk[col])
  }
  for (const change of group.changes) {
    const col = change.column_name
    if (!col || pkLower.has(col.toLowerCase())) continue
    previous[col] = displayCell(change.old_value)
    current[col] = displayCell(change.new_value)
  }

  return {
    ...group,
    pkCols,
    changedCols,
    columns,
    previous,
    current,
  }
}

/** Case-insensitive property lookup on row objects. */
function rowValue(row, col) {
  if (!row || !col) return undefined
  if (row[col] !== undefined) return row[col]
  const lower = String(col).toLowerCase()
  const key = Object.keys(row).find(k => k.toLowerCase() === lower)
  return key ? row[key] : undefined
}

/**
 * Group approver diffs into record-level before/after blocks (one card per record).
 * Uses business key columns when configured; falls back to primary keys.
 */
export function groupApprovalDiffsForComparison(
  diffs,
  { pkCols = [], businessKeyCols = [] } = {},
) {
  const groups = new Map()
  const pkLower = new Set(pkCols.map(c => c.toLowerCase()))
  const identityCols = dedupeColumns([
    ...(businessKeyCols.length ? businessKeyCols : []),
    ...pkCols,
  ])

  for (const d of diffs || []) {
    const pk = d.pk || {}
    const pkKey = JSON.stringify(pk, Object.keys(pk).sort())
    if (!groups.has(pkKey)) {
      let before = {}
      let after = {}
      try {
        before = typeof d.before_row_json === 'string'
          ? JSON.parse(d.before_row_json || '{}')
          : (d.before_row_json || {})
      } catch { /* keep empty */ }
      try {
        after = typeof d.after_row_json === 'string'
          ? JSON.parse(d.after_row_json || '{}')
          : (d.after_row_json || {})
      } catch { /* keep empty */ }
      groups.set(pkKey, {
        pk,
        operation: d.operation || 'update',
        row: d.row,
        before,
        after,
        items: [],
      })
    }
    const group = groups.get(pkKey)
    group.items.push(d)
    if (d.operation === 'insert') group.operation = 'insert'
  }

  return Array.from(groups.values())
    .map(group => {
      const changedCols = []
      for (const item of group.items) {
        const col = item.column
        if (!col || pkLower.has(String(col).toLowerCase())) continue
        if (changedCols.some(c => c.toLowerCase() === String(col).toLowerCase())) continue
        changedCols.push(col)
      }

      const displayIdentity = identityCols.length ? identityCols : Object.keys(group.pk)
      const columns = dedupeColumns([...displayIdentity, ...changedCols])

      const previous = {}
      const current = {}
      for (const col of displayIdentity) {
        const val = rowValue(group.pk, col)
          ?? rowValue(group.before, col)
          ?? rowValue(group.after, col)
        previous[col] = displayCell(val)
        current[col] = displayCell(val)
      }
      for (const col of changedCols) {
        const item = group.items.find(i => String(i.column).toLowerCase() === col.toLowerCase())
        previous[col] = displayCell(rowValue(group.before, col) ?? item?.old)
        current[col] = displayCell(rowValue(group.after, col) ?? item?.new)
      }

      const businessKeyLabel = (businessKeyCols.length ? businessKeyCols : pkCols)
        .map(col => `${col}=${previous[col] ?? current[col] ?? '(empty)'}`)
        .join(' · ')
      const pkLabel = Object.entries(group.pk)
        .map(([k, v]) => `${k}=${displayCell(v)}`)
        .join(' · ')
      const recordLabel = businessKeyLabel || pkLabel || `Row ${group.row ?? '?'}`

      return {
        ...group,
        pkCols: displayIdentity,
        changedCols,
        columns,
        previous,
        current,
        recordLabel,
        businessKeyLabel: businessKeyLabel || pkLabel,
      }
    })
    .sort((a, b) => Number(a.row || 0) - Number(b.row || 0))
}

function dedupeColumns(cols) {
  const seen = new Set()
  const out = []
  for (const col of cols) {
    const key = String(col).toLowerCase()
    if (!col || seen.has(key)) continue
    seen.add(key)
    out.push(col)
  }
  return out
}

/** Resolve display label from table column config when available. */
export function columnLabel(columns, columnName) {
  const match = (columns || []).find(
    c => String(c.column_name).toLowerCase() === String(columnName).toLowerCase()
  )
  return match?.display_label || columnName
}

/**
 * Group pending review changes into before/after table blocks (one row each).
 * Used in the save-review screen.
 */
export function groupReviewChangesForComparison(changes, pkCols = []) {
  const groups = new Map()

  for (const change of changes) {
    const key = change.row_pk
    if (!groups.has(key)) {
      groups.set(key, {
        row_pk: change.row_pk,
        pk_values: change.pk_values || {},
        items: [],
      })
    }
    groups.get(key).items.push(change)
  }

  return Array.from(groups.values()).map(group => {
    const pkLower = new Set(pkCols.map(c => c.toLowerCase()))
    const changedCols = []

    for (const item of group.items) {
      const col = item.column_name
      if (!col) continue
      if (pkLower.has(col.toLowerCase())) continue
      if (changedCols.some(c => c.toLowerCase() === col.toLowerCase())) continue
      changedCols.push(col)
    }

    const rowKeyCol = 'Row key'
    const columns = pkCols.length ? [...pkCols, ...changedCols] : [rowKeyCol, ...changedCols]
    const resolvedPkCols = pkCols.length ? pkCols : [rowKeyCol]
    const previous = {}
    const current = {}

    if (pkCols.length) {
      for (const pk of pkCols) {
        previous[pk] = displayCell(group.pk_values[pk])
        current[pk] = displayCell(group.pk_values[pk])
      }
    } else {
      previous[rowKeyCol] = displayCell(group.row_pk)
      current[rowKeyCol] = displayCell(group.row_pk)
    }

    for (const item of group.items) {
      const col = item.column_name
      if (!col || pkLower.has(col.toLowerCase())) continue
      previous[col] = displayCell(item.old_value)
      current[col] = displayCell(item.new_value)
    }

    return {
      ...group,
      pkCols: resolvedPkCols,
      changedCols,
      columns,
      previous,
      current,
    }
  })
}

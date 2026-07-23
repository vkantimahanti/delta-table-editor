/** Columns fixed from registry config — PK and all configured visible columns. */
export function configColumnNames(columns) {
  if (!columns?.length) return []
  return columns.map(c => c.column_name)
}

/** Visible columns plus hidden PK/version required for optimistic-lock updates. */
export function dataFetchColumnNames(allColumns, visibleColumns) {
  const names = new Set(visibleColumns || [])
  for (const col of allColumns || []) {
    if (col.is_pk || col.column_name === 'version') {
      names.add(col.column_name)
    }
  }
  return [...names]
}

export function isLockedColumn(col) {
  return Boolean(col?.is_pk || col?.is_mandatory)
}

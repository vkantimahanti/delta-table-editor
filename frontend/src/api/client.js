/**
 * API client — all calls to FastAPI backend.
 * Base URL is empty (same origin) — Vite proxies /api → localhost:8000 in dev.
 */

function formatDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(item => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object') {
        const parts = [item.reason, item.fix].filter(Boolean)
        if (parts.length) return parts.join(' ')
        return item.column || JSON.stringify(item)
      }
      return String(item)
    }).join('\n')
  }
  if (typeof detail === 'object') {
    if (Array.isArray(detail.errors)) {
      return detail.errors.slice(0, 8).map(e => {
        if (Array.isArray(e.duplicate_rows) && e.duplicate_rows.length) {
          const pk = e.pk && typeof e.pk === 'object'
            ? Object.entries(e.pk).map(([k, v]) => `${k}=${v || '(blank)'}`).join(', ')
            : ''
          const rowLabel = `Rows ${e.duplicate_rows.join(', ')}`
          return `${rowLabel}${pk ? ` (${pk})` : ''}: ${e.reason || ''}${e.fix ? ` — ${e.fix}` : ''}`
        }
        const row = e.row ? `Row ${e.row}: ` : ''
        return `${row}${e.reason || ''}${e.fix ? ` — ${e.fix}` : ''}`
      }).join('\n')
    }
    if (detail.message) return detail.message
    if (detail.reason) return detail.reason
  }
  return String(detail)
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  if (!res.ok) {
    let msg = await res.text()
    let detailObj = null
    try {
      const p = JSON.parse(msg)
      detailObj = p.detail
      msg = formatDetail(p.detail) || msg
    } catch (_) {}
    const err = new Error(msg || `HTTP ${res.status}`)
    err.status = res.status
    err.conflict = res.status === 409
    err.rateLimited = res.status === 429
    if (detailObj && typeof detailObj === 'object') err.detail = detailObj
    throw err
  }
  return res.json()
}

const get  = (path)        => request(path)
const post = (path, body)  => request(path, { method: 'POST',   body: JSON.stringify(body) })
const patch= (path, body)  => request(path, { method: 'PATCH',  body: JSON.stringify(body) })
const del  = (path, body)  => request(path, { method: 'DELETE', body: JSON.stringify(body) })

export const api = {
  health:       ()           => get('/api/health'),
  authStatus:   ()           => get('/api/auth/status'),
  catalogs:     ()           => get('/api/catalogs'),
  schemas:      (cat)        => get(`/api/catalogs/${cat}/schemas`),
  tables:       (cat, sch)   => get(`/api/catalogs/${cat}/schemas/${sch}/tables`),
  getTables:    ()           => get('/api/tables'),
  getGroups:    ()           => get('/api/groups'),
  columns:      (sch, tbl, catalog = '') =>
    get(`/api/tables/${sch}/${tbl}/columns?catalog=${encodeURIComponent(catalog)}`),
  dropdowns:    (sch, tbl)   => get(`/api/tables/${sch}/${tbl}/dropdowns`),

  getData: (sch, tbl, { catalog = '', columns = '*', page = 1, page_size = 500 } = {}) =>
    get(`/api/tables/${sch}/${tbl}/data?catalog=${catalog}&columns=${encodeURIComponent(columns)}&page=${page}&page_size=${page_size}`),

  filterData: (sch, tbl, filters, { catalog = '', columns = '*', page = 1, page_size = 500 } = {}) =>
    post(`/api/tables/${sch}/${tbl}/filter?catalog=${catalog}&columns=${encodeURIComponent(columns)}&page=${page}&page_size=${page_size}`, filters),

  validate: (sch, tbl, body) => post(`/api/tables/${sch}/${tbl}/validate`, body),

  updateRow: (sch, tbl, body)   => patch(`/api/tables/${sch}/${tbl}/row`, body),
  insertRow: (sch, tbl, body)   => post(`/api/tables/${sch}/${tbl}/row`, body),
  deleteRow: (sch, tbl, pkVals, soft = true) =>
    del(`/api/tables/${sch}/${tbl}/row?soft=${soft}`, pkVals),

  history: (sch, tbl, limit = 300) => get(`/api/tables/${sch}/${tbl}/history?limit=${limit}`),

  uploadCheck: (catalog, schema, table) =>
    get(`/api/upload/check?catalog=${catalog}&schema=${schema}&table=${table}`),
  upload: (body) => post('/api/upload', body),

  uploadValidate: (sch, tbl, body) =>
    post(`/api/tables/${sch}/${tbl}/upload/validate`, body),
  uploadApply: (sch, tbl, changeRequestId) =>
    post(`/api/tables/${sch}/${tbl}/upload/apply`, { change_request_id: changeRequestId }),
  stageGridEdits: (sch, tbl, body) =>
    post(`/api/tables/${sch}/${tbl}/edits/stage`, body),
  applyGridEdits: (sch, tbl, changeRequestId) =>
    post(`/api/tables/${sch}/${tbl}/edits/apply`, { change_request_id: changeRequestId }),
  getChangeRequestDiffs: (id) => get(`/api/change-requests/${id}/diffs`),
  getChangeRequest: (id) => get(`/api/change-requests/${id}`),
  getOverview: (refresh = false) =>
    get(`/api/overview${refresh ? '?refresh=true' : ''}`),
  listPendingChangeRequests: ({ page = 1, pageSize = 20, schemaName = '', tableName = '' } = {}) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (schemaName) params.set('schema_name', schemaName)
    if (tableName) params.set('table_name', tableName)
    return get(`/api/change-requests/pending?${params}`)
  },
  getChangeRequestReviewSql: (id) => get(`/api/change-requests/${id}/review-sql`),
  approveChangeRequest: (id) => post(`/api/change-requests/${id}/approve`, {}),
  rejectChangeRequest: (id, reason = '') =>
    post(`/api/change-requests/${id}/reject`, { reason }),
  getApprovalReview: (token) =>
    get(`/api/approvals/review?token=${encodeURIComponent(token)}`),

  exportData: (sch, tbl, body) =>
    post(`/api/tables/${sch}/${tbl}/export`, body),
  downloadExport: (changeRequestId) =>
    `/api/exports/${encodeURIComponent(changeRequestId)}/download`,

  refreshRules: () => post('/api/admin/rules/refresh', {}),
}

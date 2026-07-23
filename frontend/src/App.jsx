import React, { useState, useEffect, useCallback, useRef } from 'react'
import { api } from './api/client.js'
import Sidebar from './components/Sidebar.jsx'
import Breadcrumb, { buildBreadcrumbItems } from './components/Breadcrumb.jsx'
import Overview from './components/Overview.jsx'
import WorkspaceHeader from './components/WorkspaceHeader.jsx'
import TabBar from './components/TabBar.jsx'
import DataGrid from './components/DataGrid.jsx'
import StartupScreen from './components/StartupScreen.jsx'
import { useWarehouseStartup } from './hooks/useWarehouseStartup.js'
import {
  ReviewPanel, UploadPanel, HistoryPanel, ApprovalsPanel,
  ExportPanel, ExportConfirmDialog, EXPORT_CONFIRM_ROW_LIMIT,
} from './components/Panels.jsx'
import { configColumnNames, dataFetchColumnNames } from './utils/columns.js'
import { readUploadFile } from './utils/uploadFile.js'
import styles from './App.module.css'

const PAGE_SIZE = 500
const FALLBACK_CATALOG = 'your_catalog'

function tableCatalog(row) {
  return String(row?.catalog || FALLBACK_CATALOG).trim()
}

function registryForGroup(rows, group) {
  if (!Array.isArray(rows)) return []
  if (group === 'All') return rows
  return rows.filter(r => String(r.app_group || '') === group)
}

function catalogsFromRegistry(rows, group) {
  return [...new Set(registryForGroup(rows, group).map(tableCatalog))].sort()
}

function schemasFromRegistry(rows, group, catalog) {
  return [...new Set(
    registryForGroup(rows, group)
      .filter(r => tableCatalog(r) === catalog)
      .map(r => r.schema_name)
      .filter(Boolean),
  )].sort()
}

function tablesFromRegistry(rows, group, catalog, schema) {
  return registryForGroup(rows, group)
    .filter(r => tableCatalog(r) === catalog && r.schema_name === schema)
    .map(r => ({
      table_name: r.table_name,
      app_group: r.app_group || '',
      display_name: r.display_name || r.table_name,
    }))
    .sort((a, b) => String(a.display_name).localeCompare(String(b.display_name)))
}

function applyPageResult(setters, result) {
  const rows = result.rows || []
  setters.setRows(Array.isArray(rows) ? rows : [])
  setters.setTotalCount(result.total_count ?? null)
  setters.setCurrentPage(result.page ?? 1)
  setters.setHasMore(Boolean(result.has_more))
  return rows
}

export default function App() {
  const { warehouseReady, elapsed, statusMessage, showSlowWarning, retryNow } = useWarehouseStartup()
  /* ── nav state ── */
  const [catalogs, setCatalogs] = useState([])
  const [schemas,  setSchemas]  = useState([])
  const [tables,   setTables]   = useState([])
  const [catalog,  setCatalog]  = useState('')
  const [schema,   setSchema]   = useState('')
  const [table,    setTable]    = useState('')
  const [selectedGroup, setSelectedGroup] = useState('All')
  const [groups, setGroups] = useState(['All'])

  /* ── column config ── */
  const [allColumns,     setAllColumns]     = useState([])
  const [visibleColumns, setVisibleColumns] = useState([])
  const [dropdownCache,  setDropdownCache]  = useState({})
  const [dependentDropdowns, setDependentDropdowns] = useState({})

  /* ── data ── */
  const [rows,       setRows]       = useState([])
  const [editedRows, setEditedRows] = useState({})   // { idx: { col: val } }
  const [newRowIdxs, setNewRowIdxs] = useState(new Set())
  const [selectedRows, setSelectedRows] = useState(new Set())
  const [colFilters, setColFilters] = useState({})
  const [totalCount, setTotalCount] = useState(null)
  const [currentPage, setCurrentPage] = useState(1)
  const [hasMore, setHasMore] = useState(false)

  /* ── history ── */
  const [history,       setHistory]       = useState([])
  const [deltaVersion,  setDeltaVersion]  = useState('latest')

  /* ── ui state ── */
  const [activeSection, setActiveSection] = useState('overview')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [pendingApprovalCount, setPendingApprovalCount] = useState(0)
  const [activeTab,   setActiveTab]   = useState('data')
  const [showReview,  setShowReview]  = useState(false)
  const [loading,     setLoading]     = useState(false)
  const [status,      setStatus]      = useState(null)  // { msg, type }
  const [exportConfirm, setExportConfirm] = useState(null)  // { fmt, rowCount } | null
  const [approvalTokenReview, setApprovalTokenReview] = useState(null)

  /* ── review state ── */
  const [reviewChanges,  setReviewChanges]  = useState([])
  const [reviewBlocking, setReviewBlocking] = useState([])
  const [reviewWarnings, setReviewWarnings] = useState([])

  /* ── upload ── */
  const [uploadFile, setUploadFile] = useState(null)
  const fileInputRef = useRef(null)
  const filterTimerRef = useRef(null)
  const registryTablesRef = useRef([])
  const [registryTables, setRegistryTables] = useState([])

  /* ── derived ── */
  const pkCols = allColumns.filter(c => c.is_pk).map(c => c.column_name)

  const visColMeta = allColumns.filter(c => visibleColumns.includes(c.column_name))

  const filteredTables = selectedGroup === 'All'
    ? tables
    : tables.filter(t => t.app_group === selectedGroup)

  const tableOptions = filteredTables.map(t => t.table_name)

  const activeTableMeta = registryTables.find(
    r => r.schema_name === schema && r.table_name === table,
  )
  const breadcrumbItems = buildBreadcrumbItems({
    activeSection,
    selectedGroup,
    catalog,
    schema,
    table,
    tableLabel: activeTableMeta?.display_name || table,
    activeTab: activeSection === 'workspace' ? activeTab : null,
  })

  const displayRows = rows
    .map((row, sourceIdx) => ({ row, sourceIdx }))

  const changedCount = Object.keys(editedRows).length
  const activeFilters = Object.entries(colFilters)
    .filter(([, v]) => v)
    .map(([column, value]) => ({ column, value }))

  const totalPages = totalCount != null ? Math.max(1, Math.ceil(totalCount / PAGE_SIZE)) : null
  const pageSetters = { setRows, setTotalCount, setCurrentPage, setHasMore }

  function msg(text, type = 'info') { setStatus({ msg: text, type }) }

  async function loadGroups() {
    try {
      const result = await api.getGroups()
      setGroups(result.groups || ['All'])
    } catch {
      setGroups(['All'])
    }
  }

  /* ── startup ── */
  useEffect(() => {
    if (!warehouseReady) return
    loadGroups()
    api.getTables()
      .then(data => {
        const rows = Array.isArray(data) ? data : []
        registryTablesRef.current = rows
        setRegistryTables(rows)
        setCatalogs(catalogsFromRegistry(rows, selectedGroup))
      })
      .catch(async (e) => {
        registryTablesRef.current = []
        setRegistryTables([])
        setCatalogs([])
        sessionStorage.removeItem('data-canvas-warehouse-ready')
        let detail = e.message
        try {
          const auth = await api.authStatus()
          if (!auth.user_token_present) {
            detail = 'No user token forwarded. Enable User authorization with the sql scope, restart the app, and re-open it.'
          } else if (!auth.sql_scope_present) {
            detail = `Login token missing sql scope (has: ${auth.token_scopes.join(', ') || 'none'}). Log out of Databricks, re-open the app, and accept consent.`
          }
        } catch (_) { /* use original message */ }
        msg(`Could not load registered tables: ${detail}`, 'error')
      })
  }, [warehouseReady])

  useEffect(() => {
    if (!warehouseReady || catalog || !registryTables.length) return
    const catList = catalogsFromRegistry(registryTables, selectedGroup)
    if (catList.length) onCatalog(catList[0], selectedGroup)
  }, [warehouseReady, registryTables, catalog, selectedGroup])

  useEffect(() => () => clearTimeout(filterTimerRef.current), [])

  useEffect(() => {
    if (!warehouseReady) return
    const params = new URLSearchParams(window.location.search)
    const tab = params.get('tab')
    const token = params.get('token')
    if (tab === 'approvals') setActiveSection('approvals')
    if (token) {
      api.getApprovalReview(token)
        .then(data => {
          setApprovalTokenReview(data)
          setActiveSection('approvals')
        })
        .catch(e => msg(`Approval link: ${e.message}`, 'error'))
    } else if (!tab) {
      setActiveSection('overview')
    }
  }, [warehouseReady])

  useEffect(() => {
    if (!warehouseReady) return
    api.listPendingChangeRequests({ page: 1, pageSize: 1 })
      .then(result => setPendingApprovalCount(Number(result?.total) || 0))
      .catch(() => setPendingApprovalCount(0))
  }, [warehouseReady, activeSection])

  function onSidebarNavigate(section) {
    if (section === 'overview') {
      setActiveSection('overview')
      return
    }
    if (section === 'approvals') {
      setActiveSection('approvals')
      return
    }
    if (section === 'workspace') {
      setActiveSection('workspace')
    }
  }

  async function openTableInEditor(sch, tbl) {
    setActiveSection('workspace')
    setActiveTab('data')
    const reg = registryTablesRef.current.find(
      r => r.schema_name === sch && r.table_name === tbl,
    )
    const group = reg?.app_group || selectedGroup
    const cat = reg ? tableCatalog(reg) : (catalog || catalogs[0])
    if (!cat) {
      msg('Registered tables are still loading. Try again in a moment.', 'error')
      return
    }
    if (group !== selectedGroup) {
      onGroupChange(group)
      onSchema(sch, group)
    } else {
      if (catalog !== cat) onCatalog(cat, group)
      if (schema !== sch) onSchema(sch, group)
    }
    if (table !== tbl) await onTable(tbl)
    else await loadTable(tbl)
  }

  function onWorkspaceTabChange(tab) {
    setActiveTab(tab)
    if (activeSection !== 'workspace') setActiveSection('workspace')
  }

  useEffect(() => {
    const stillVisible = filteredTables.some(t => t.table_name === table)
    if (!stillVisible && table) {
      setTable('')
      setSchema('')
      setRows([])
      setTotalCount(null)
    }
  }, [selectedGroup, filteredTables, table])

  function onGroupChange(group) {
    setSelectedGroup(group)
    const rows = registryTablesRef.current
    const catList = catalogsFromRegistry(rows, group)
    setCatalogs(catList)
    if (!catList.length) {
      setCatalog('')
      setSchema('')
      setTable('')
      setSchemas([])
      setTables([])
      resetTableState()
      return
    }
    const nextCat = catList.includes(catalog) ? catalog : catList[0]
    onCatalog(nextCat, group)
  }

  /* ── nav handlers (registry-driven — group filters catalog/schema/table) ── */
  function onCatalog(cat, group = selectedGroup) {
    setCatalog(cat)
    setSchema('')
    setTable('')
    setTables([])
    resetTableState()
    if (!cat) {
      setSchemas([])
      return
    }
    setSchemas(schemasFromRegistry(registryTablesRef.current, group, cat))
  }

  function onSchema(sch, group = selectedGroup) {
    setSchema(sch)
    setTable('')
    resetTableState()
    if (!sch) {
      setTables([])
      return
    }
    setTables(tablesFromRegistry(registryTablesRef.current, group, catalog, sch))
  }

  async function onTable(tbl) {
    setTable(tbl); resetTableState()
    if (!tbl) return
    await loadTable(tbl)
  }

  function resetTableState() {
    setRows([]); setEditedRows({}); setNewRowIdxs(new Set())
    setSelectedRows(new Set()); setColFilters({})
    setTotalCount(null); setCurrentPage(1); setHasMore(false)
    setAllColumns([]); setVisibleColumns([])
    setDropdownCache({}); setHistory([])
    setShowReview(false)
    setUploadFile(null)
  }

  function onBrowseUploadFile() {
    if (!table) {
      msg('Select catalog, schema, and table before choosing a file.', 'error')
      return
    }
    const input = fileInputRef.current
    if (!input) return
    input.value = ''
    input.click()
  }

  function onUploadFileSelected(e) {
    const f = e.target.files?.[0]
    if (!f) return
    readUploadFile(f)
      .then(file => setUploadFile(file))
      .catch(err => {
        setUploadFile(null)
        msg(err.message || 'Could not read upload file.', 'error')
      })
  }

  function inferColumnsFromRows(data) {
    if (!data.length) return []
    return Object.keys(data[0]).map((name, i) => ({
      column_name: name,
      display_label: name,
      col_order: i + 1,
      col_type: 'string',
      is_visible: true,
      is_editable: true,
      is_mandatory: false,
      is_filter: false,
      is_pk: false,
    }))
  }

  function parseDropdownResponse(ddRes) {
    if (!ddRes || typeof ddRes !== 'object') {
      return { dropdowns: {}, dependent: {} }
    }
    if (ddRes.dropdowns || ddRes.dependent) {
      return {
        dropdowns: ddRes.dropdowns ?? {},
        dependent: ddRes.dependent ?? {},
      }
    }
    // Legacy flat map: { Carrier: [...] } — dependent data not included
    return { dropdowns: ddRes, dependent: {} }
  }

  async function loadTable(tbl) {
    setLoading(true)
    try {
      const [cols, ddRes, hist] = await Promise.all([
        api.columns(schema, tbl, catalog).catch(() => []),
        api.dropdowns(schema, tbl).catch(() => ({})),
        api.history(schema, tbl).catch(() => []),
      ])
      const parsed = parseDropdownResponse(ddRes)
      setDropdownCache(parsed.dropdowns)
      setDependentDropdowns(parsed.dependent)
      setHistory(hist)

      let resolvedCols = cols
      const visible = configColumnNames(resolvedCols)
      const colStr = dataFetchColumnNames(resolvedCols, visible).join(', ') || '*'
      const result = await api.getData(schema, tbl, {
        catalog, columns: colStr, page: 1, page_size: PAGE_SIZE,
      })
      const data = applyPageResult(pageSetters, result)

      if (!resolvedCols.length && data.length) {
        resolvedCols = inferColumnsFromRows(data)
      }

      setAllColumns(resolvedCols)
      setVisibleColumns(configColumnNames(resolvedCols))

      if (!resolvedCols.length) {
        msg(`Loaded ${data.length} rows but no columns found for ${schema}.${tbl}`, 'error')
      } else {
        const total = result.total_count ?? data.length
        msg(`Loaded page 1 of ${Math.ceil(total / PAGE_SIZE) || 1} (${total.toLocaleString()} rows) from ${schema}.${tbl}`, 'success')
      }
    } catch (e) {
      msg(`Load failed: ${e.message}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  async function loadTableData(page = 1) {
    if (!table) return
    setLoading(true)
    try {
      const visible = visibleColumns.length
        ? visibleColumns
        : configColumnNames(allColumns)
      const colStr = dataFetchColumnNames(allColumns, visible).join(', ') || '*'
      const result = await api.getData(schema, table, {
        catalog, columns: colStr, page, page_size: PAGE_SIZE,
      })
      applyPageResult(pageSetters, result)
    } catch (e) {
      msg(`Load failed: ${e.message}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  async function fetchWithFilters(filters, page = 1) {
    if (!table) return
    const active = Object.entries(filters)
      .filter(([, v]) => v && String(v).trim() !== '')
      .map(([column, value]) => ({ column, value }))

    if (active.length === 0) {
      setCurrentPage(page)
      await loadTableData(page)
      return
    }

    setLoading(true)
    try {
      const visible = visibleColumns.length
        ? visibleColumns
        : configColumnNames(allColumns)
      const colStr = dataFetchColumnNames(allColumns, visible).join(', ') || '*'
      const result = await api.filterData(schema, table, active, {
        catalog,
        columns: colStr,
        page,
        page_size: PAGE_SIZE,
      })
      applyPageResult(pageSetters, result)
    } catch (err) {
      if (err.rateLimited) {
        msg('Too many filter requests. Please wait a moment.', 'error')
      } else {
        msg(err.message, 'error')
      }
    } finally {
      setLoading(false)
    }
  }

  async function goToPage(page) {
    if (page < 1 || loading) return
    if (totalPages != null && page > totalPages) return
    setEditedRows({})
    setNewRowIdxs(new Set())
    setSelectedRows(new Set())
    await fetchWithFilters(colFilters, page)
  }

  async function onRefresh() {
    if (!table) return
    resetTableState()
    await loadTable(table)
  }

  /* ── column filter ── */
  function onColFilter(col, val) {
    const updated = { ...colFilters, [col]: val }
    setColFilters(updated)
    clearTimeout(filterTimerRef.current)
    filterTimerRef.current = setTimeout(() => {
      fetchWithFilters(updated, 1)
    }, 400)
  }
  function onRemoveFilter(col) {
    const updated = { ...colFilters }
    delete updated[col]
    setColFilters(updated)
    fetchWithFilters(updated, 1)
  }

  /* ── selection ── */
  function onToggleRow(idx, checked) {
    setSelectedRows(prev => {
      const n = new Set(prev)
      checked ? n.add(idx) : n.delete(idx)
      return n
    })
  }
  function onToggleAll(checked) {
    const visibleIdxs = displayRows.map(({ sourceIdx }) => sourceIdx)
    setSelectedRows(prev => {
      if (!checked) {
        const visible = new Set(visibleIdxs)
        return new Set([...prev].filter(i => !visible.has(i)))
      }
      return new Set([...prev, ...visibleIdxs])
    })
  }

  function rowLabel(row, sourceIdx) {
    if (pkCols.length) {
      return pkCols.map(pk => row[pk] ?? '').filter(Boolean).join(' / ') || `row ${sourceIdx + 1}`
    }
    return `row ${sourceIdx + 1}`
  }

  function rowIsAutoPkInsert(row) {
    if (!pkCols.length) return false
    const blankPks = pkCols.every(pk => !String(row?.[pk] ?? '').trim())
    if (!blankPks) return false
    return pkCols.every(pk => {
      const meta = allColumns.find(c => c.column_name === pk)
      return meta && !meta.is_editable
    })
  }

  function isNewGridRow(idx) {
    return newRowIdxs.has(idx) || rowIsAutoPkInsert(rows[idx])
  }

  /* ── cell edit ── */
  function onCellChange(sourceIdx, col, val) {
    setEditedRows(prev => {
      const prior = prev[sourceIdx] || {}
      const rowEdits = { ...prior, [col]: val }
      Object.entries(dependentDropdowns).forEach(([childCol, meta]) => {
        if (meta?.parent_column === col) {
          const priorParent = prior[col] !== undefined ? prior[col] : rows[sourceIdx]?.[col]
          if (String(val ?? '') !== String(priorParent ?? '')) {
            rowEdits[childCol] = ''
          }
        }
      })
      return { ...prev, [sourceIdx]: rowEdits }
    })
  }

  /* ── add row ── */
  function onAdd() {
    const blank = {}
    allColumns.forEach(c => { blank[c.column_name] = '' })
    const newIdx = rows.length
    setRows(prev => [...prev, blank])
    setNewRowIdxs(prev => new Set([...prev, newIdx]))
    setEditedRows(prev => ({ ...prev, [newIdx]: blank }))
  }

  /* ── delete ── */
  async function onDelete() {
    if (!selectedRows.size) return

    const existingIdxs = []
    const newOnlyIdxs = []
    selectedRows.forEach(idx => {
      if (newRowIdxs.has(idx)) newOnlyIdxs.push(idx)
      else existingIdxs.push(idx)
    })

    if (existingIdxs.length) {
      const noun = existingIdxs.length === 1 ? 'row' : 'rows'
      if (!window.confirm(`Delete ${existingIdxs.length} ${noun} from the database? This cannot be undone.`)) {
        return
      }
    }

    const selected = new Set(selectedRows)
    setLoading(true)
    try {
      for (const idx of existingIdxs) {
        const row = rows[idx] || {}
        const pkVals = {}
        pkCols.forEach(pk => { pkVals[pk] = row[pk] })
        await api.deleteRow(schema, table, pkVals, false)
      }

      setRows(prev => prev.filter((_, i) => !selected.has(i)))
      setEditedRows(prev => {
        const n = {}
        Object.entries(prev).forEach(([k, v]) => {
          if (!selected.has(Number(k))) n[k] = v
        })
        return n
      })
      setNewRowIdxs(prev => {
        const n = new Set(prev)
        selected.forEach(i => n.delete(i))
        return n
      })
      setSelectedRows(new Set())

      if (existingIdxs.length) {
        msg(`Deleted ${existingIdxs.length} row(s).`, 'success')
        await loadTable(table)
      } else if (newOnlyIdxs.length) {
        msg(`Removed ${newOnlyIdxs.length} unsaved row(s).`, 'info')
      }
    } catch (e) {
      msg(`Delete failed: ${e.message}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  function violationKey(v) {
    if (typeof v === 'string') return v
    return `${v.column || ''}|${v.reason || ''}|${v.fix || ''}`
  }

  /* ── review & save ── */
  async function onReview() {
    const changes = []
    Object.entries(editedRows).forEach(([idxStr, edits]) => {
      const idx = Number(idxStr)
      const original = isNewGridRow(idx) ? {} : (rows[idx] || {})
      visColMeta.forEach(col => {
        const ov = String(original[col.column_name] ?? '')
        const nv = String(edits[col.column_name] ?? original[col.column_name] ?? '')
        if (ov !== nv) {
          const pkValues = {}
          pkCols.forEach(pk => {
            pkValues[pk] = String(original[pk] ?? edits[pk] ?? '')
          })
          changes.push({
            row_pk: rowLabel(original, idx),
            pk_values: pkValues,
            column_name: col.column_name,
            column: col.display_label || col.column_name,
            old_value: ov,
            new_value: nv,
          })
        }
      })
    })
    if (!changes.length) { msg('No changes to save.', 'info'); return }

    // Run validation
    const allBlocking = [], allWarnings = []
    for (const [idxStr, edits] of Object.entries(editedRows)) {
      const idx = Number(idxStr)
      if (isNewGridRow(idx)) continue
      try {
        const r = await api.validate(schema, table, {
          original: rows[idx], edits,
          pk_cols: pkCols,
          editable_cols: visColMeta.filter(c => c.is_editable).map(c => c.column_name),
          mandatory_cols: allColumns.filter(c => c.is_mandatory).map(c => c.column_name),
        })
        allBlocking.push(...(r.blocking || []))
        allWarnings.push(...(r.warnings || []))
      } catch (_) {}
    }

    setReviewChanges(changes)
    setReviewBlocking([...new Map(allBlocking.map(v => [violationKey(v), v])).values()])
    setReviewWarnings([...new Map(allWarnings.map(v => [violationKey(v), v])).values()])
    setShowReview(true)
  }

  async function onConfirmSave() {
    setLoading(true)
    try {
      const updates = []
      const inserts = []
      for (const [idxStr, edits] of Object.entries(editedRows)) {
        const idx = Number(idxStr)
        if (isNewGridRow(idx)) {
          inserts.push({ values: { ...(rows[idx] || {}), ...edits } })
        } else {
          updates.push({ original: rows[idx] || {}, edits })
        }
      }

      const stageResult = await api.stageGridEdits(schema, table, {
        catalog,
        updates,
        inserts,
      })

      if (stageResult.requires_approval) {
        setShowReview(false)
        setEditedRows({})
        setNewRowIdxs(new Set())
        msg(
          `Changes staged (${stageResult.change_request_id}) — awaiting approval. Approver: Approvals tab → Approve & apply to table.`,
          'info',
        )
        return
      }

      if (stageResult.can_apply && stageResult.change_request_id) {
        const applyResult = await api.applyGridEdits(schema, table, stageResult.change_request_id)
        setShowReview(false)
        setEditedRows({})
        setNewRowIdxs(new Set())
        const rev = applyResult.revision_id ? ` · revision ${applyResult.revision_id}` : ''
        msg(
          `Applied ${applyResult.rows_updated ?? 0} update(s) and ${applyResult.rows_inserted ?? 0} insert(s)${rev}.`,
          'success',
        )
        await loadTable(table)
        return
      }

      msg('Changes staged but could not be applied.', 'error')
    } catch (e) {
      const detail = e.detail || {}
      if (Array.isArray(detail.errors)) {
        msg(detail.errors.slice(0, 3).map(er => er.reason || er).join(' | '), 'error')
      } else {
        msg(`Save failed: ${e.message}`, 'error')
      }
    } finally {
      setLoading(false)
    }
  }

  function onCancelChanges() {
    setEditedRows({})
    setNewRowIdxs(new Set())
    setRows(prev => prev.filter((_, i) => !newRowIdxs.has(i)))
    setShowReview(false)
  }

  /* ── export (server-side: all filtered rows → Volume → download) ── */
  async function doExport(fmt) {
    const serverFmt = fmt === 'excel' ? 'xlsx' : fmt === 'txt' ? 'tsv' : 'csv'
    setLoading(true)
    msg('Export in progress…', 'info')
    try {
      const result = await api.exportData(schema, table, {
        catalog,
        format: serverFmt,
        columns: visibleColumns.length ? visibleColumns : allColumns.map(c => c.column_name),
        filters: activeFilters,
        filter_snapshot: {
          filters: activeFilters,
          schema,
          table,
          catalog,
          visible_columns: visibleColumns,
        },
      })
      const href = result.download_url || api.downloadExport(result.change_request_id)
      const link = document.createElement('a')
      link.href = href
      link.rel = 'noopener'
      document.body.appendChild(link)
      link.click()
      link.remove()
      const filterNote = activeFilters.length ? ' (filtered)' : ''
      msg(
        `Export complete${filterNote} — ${Number(result.row_count || 0).toLocaleString()} rows, ${String(result.format || serverFmt).toUpperCase()}`,
        'success',
      )
    } catch (e) {
      msg(`Export failed: ${e.message}`, 'error')
    } finally {
      setLoading(false)
    }
  }

  function onExport(fmt) {
    if (!table) {
      msg('Select a table before exporting.', 'error')
      return
    }
    const rowCount = totalCount ?? displayRows.length
    if (rowCount > EXPORT_CONFIRM_ROW_LIMIT) {
      setExportConfirm({ fmt, rowCount })
      return
    }
    doExport(fmt)
  }

  function onExportConfirm() {
    if (!exportConfirm) return
    doExport(exportConfirm.fmt)
    setExportConfirm(null)
  }

  /* ── render ── */
  if (!warehouseReady) {
    return (
      <StartupScreen
        elapsed={elapsed}
        statusMessage={statusMessage}
        showSlowWarning={showSlowWarning}
        onRetry={retryNow}
      />
    )
  }

  return (
    <div className={styles.shell}>
      <Sidebar
        activeSection={activeSection}
        onNavigate={onSidebarNavigate}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(c => !c)}
        pendingApprovalCount={pendingApprovalCount}
      />

      <div className={styles.main}>
        {status && (
          <div className={`${styles.statusBar} ${styles[`status_${status.type}`]}`}>
            <span>{status.msg}</span>
            <button onClick={() => setStatus(null)} aria-label="Dismiss">✕</button>
          </div>
        )}

        {exportConfirm && (
          <ExportConfirmDialog
            rowCount={exportConfirm.rowCount}
            onConfirm={onExportConfirm}
            onCancel={() => setExportConfirm(null)}
          />
        )}

        <input
          type="file"
          ref={fileInputRef}
          accept=".csv,.txt,.xlsx,.xls"
          onChange={onUploadFileSelected}
          style={{ display: 'none' }}
          aria-hidden="true"
          tabIndex={-1}
        />

        <Breadcrumb items={breadcrumbItems} />

        {activeSection === 'workspace' && (
          <>
            <WorkspaceHeader
              catalogs={catalogs}
              schemas={schemas}
              tables={tableOptions}
              groups={groups}
              selectedGroup={selectedGroup}
              onGroupChange={onGroupChange}
              catalog={catalog}
              schema={schema}
              table={table}
              loading={loading}
              onCatalog={onCatalog}
              onSchema={onSchema}
              onTable={onTable}
              onRefresh={onRefresh}
              totalCount={totalCount}
              totalColumns={allColumns.length}
              activeFilters={activeFilters}
              onRemoveFilter={onRemoveFilter}
            />

            <TabBar
              active={activeTab}
              onChange={onWorkspaceTabChange}
              canUpdate={changedCount > 0}
              canDelete={selectedRows.size > 0}
              selectedCount={selectedRows.size}
              changedCount={changedCount}
              onAdd={onAdd}
              onUpdate={onReview}
              onDelete={onDelete}
              onSave={onReview}
              onCancelChanges={onCancelChanges}
              deltaVersion={deltaVersion}
              onDeltaVersion={setDeltaVersion}
              deltaVersions={[]}
            />
          </>
        )}

        {activeSection === 'approvals' && (
          <header className={styles.pageHeader}>
            <h1 className={styles.pageTitle}>Approvals</h1>
            <p className={styles.pageSubtitle}>
              Validate staged data and approve or reject change requests before they are applied.
            </p>
          </header>
        )}

        <main className={styles.workspace}>
          {activeSection === 'overview' && (
            <Overview
              onGoToApprovals={() => setActiveSection('approvals')}
              onGoToDataEditor={() => setActiveSection('workspace')}
              onOpenTable={openTableInEditor}
              onPendingCountChange={setPendingApprovalCount}
            />
          )}

          {activeSection === 'approvals' && (
            <div className={styles.workspaceInner}>
            <div className={styles.tabContent}>
              <ApprovalsPanel
                tokenReview={approvalTokenReview}
                onClearToken={() => setApprovalTokenReview(null)}
                onApplied={() => {
                  if (table) loadTable(table)
                  api.listPendingChangeRequests({ page: 1, pageSize: 1 })
                    .then(result => setPendingApprovalCount(Number(result?.total) || 0))
                    .catch(() => {})
                }}
              />
            </div>
            </div>
          )}

          {activeSection === 'workspace' && (
          <div className={styles.workspaceInner}>
          {!table && (
            <div className={styles.emptyState}>
              <div className={styles.emptyIcon}>⊞</div>
              <h2>Select a table to get started</h2>
              <p>Choose catalog, schema, and table above to browse and edit your data.</p>
            </div>
          )}

          {table && activeTab === 'data' && (
          <div className={styles.tabContent}>
            {showReview ? (
              <ReviewPanel
                changes={reviewChanges}
                columns={allColumns}
                pkCols={pkCols}
                blocking={reviewBlocking}
                warnings={reviewWarnings}
                loading={loading}
                onConfirm={onConfirmSave}
                onCancel={() => setShowReview(false)}
              />
            ) : (
              loading ? (
                <div className={styles.loadingGrid}>
                  {[...Array(8)].map((_, i) => (
                    <div key={i} className={styles.skeletonRow}>
                      {[...Array(visColMeta.length || 4)].map((_, j) => (
                        <div key={j} className={styles.skeletonCell} />
                      ))}
                    </div>
                  ))}
                </div>
              ) : (
                <>
                <DataGrid
                  columns={visColMeta}
                  allColumns={allColumns}
                  rowItems={displayRows}
                  editedRows={editedRows}
                  newRows={newRowIdxs}
                  selectedRows={selectedRows}
                  colFilters={colFilters}
                  dropdownCache={dropdownCache}
                  dependentDropdowns={dependentDropdowns}
                  pkCols={pkCols}
                  allowUpdate={true}
                  onCellChange={onCellChange}
                  onToggleRow={onToggleRow}
                  onToggleAll={onToggleAll}
                  onColFilter={onColFilter}
                />
                {totalCount != null && totalCount > 0 && (
                  <div className={styles.paginationBar}>
                    <button
                      type="button"
                      className={styles.pageBtn}
                      disabled={loading || currentPage <= 1}
                      onClick={() => goToPage(currentPage - 1)}
                    >
                      ← Prev
                    </button>
                    <span className={styles.pageInfo}>
                      Page {currentPage} of {totalPages}
                      {' · '}
                      {totalCount.toLocaleString()} row{totalCount === 1 ? '' : 's'}
                      {activeFilters.length > 0 ? ' (filtered)' : ''}
                    </span>
                    <button
                      type="button"
                      className={styles.pageBtn}
                      disabled={loading || !hasMore}
                      onClick={() => goToPage(currentPage + 1)}
                    >
                      Next →
                    </button>
                  </div>
                )}
                </>
              )
            )}
          </div>
        )}

        {table && activeTab === 'upload' && (
          <div className={styles.tabContent}>
            <UploadPanel
              schema={schema}
              table={table}
              catalog={catalog}
              uploadFile={uploadFile}
              onUploadFileChange={setUploadFile}
              onBrowse={onBrowseUploadFile}
              onUploadSuccess={() => { loadTable(table); setUploadFile(null) }}
            />
          </div>
        )}

        {table && activeTab === 'export' && (
          <div className={styles.tabContent}>
            <ExportPanel
              table={table}
              schema={schema}
              catalog={catalog}
              activeFilters={activeFilters}
              totalCount={totalCount}
              onExport={onExport}
              loading={loading}
            />
          </div>
        )}

        {table && activeTab === 'history' && (
          <div className={styles.tabContent}>
            <HistoryPanel history={history} columns={allColumns} />
          </div>
        )}
          </div>
          )}
        </main>
      </div>
    </div>
  )
}

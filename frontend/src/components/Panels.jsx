import React from 'react'
import { AlertCircle, Check, FolderOpen, X, FileDown, FileType2, AlignLeft, Download } from 'lucide-react'
import { api } from '../api/client.js'
import { parseCsvLine, detectPasteDelimiter } from '../utils/csvParse.js'
import { uploadDataRowCount, uploadPreview } from '../utils/uploadFile.js'
import {
  formatHistoryTimestamp,
  groupHistoryForComparison,
  groupReviewChangesForComparison,
  groupApprovalDiffsForComparison,
  columnLabel,
} from '../utils/historyGroup.js'
import styles from './Panels.module.css'

const EXPORT_CONFIRM_ROW_LIMIT = 5000

/* ── Export confirm dialog ────────────────────────────────────────────────── */
export function ExportConfirmDialog({ rowCount, onConfirm, onCancel }) {
  return (
    <div className={styles.modalOverlay} onClick={onCancel}>
      <div
        className={styles.modalDialog}
        onClick={e => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-confirm-title"
      >
        <h3 id="export-confirm-title" className={styles.modalTitle}>Large export</h3>
        <p className={styles.modalBody}>
          You are about to export {rowCount.toLocaleString()} matching rows from the server
          (all rows that match your current filters, not just this page).
          The maximum export size is configurable (default 10,000 rows).
          Continue?
        </p>
        <div className={styles.modalActions}>
          <button type="button" className={`${styles.btn} ${styles.btnOutline}`} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={`${styles.btn} ${styles.btnPrimary}`} onClick={onConfirm}>
            Export anyway
          </button>
        </div>
      </div>
    </div>
  )
}

export { EXPORT_CONFIRM_ROW_LIMIT }

/* ── Export panel ─────────────────────────────────────────────────────────── */
export function ExportPanel({
  table, schema, catalog, activeFilters, totalCount, onExport, loading,
}) {
  const filterNote = activeFilters.length
    ? `${activeFilters.length} active filter${activeFilters.length === 1 ? '' : 's'}`
    : 'No filters applied'

  const formats = [
    { fmt: 'csv', label: 'CSV', desc: 'Comma-separated values', Icon: FileDown },
    { fmt: 'excel', label: 'Excel', desc: 'Microsoft Excel (.xlsx)', Icon: FileType2 },
    { fmt: 'txt', label: 'Text (TSV)', desc: 'Tab-separated values', Icon: AlignLeft },
  ]

  return (
    <div className={styles.exportPanel}>
      <div className={styles.exportHead}>
        <Download size={20} />
        <div>
          <h2 className={styles.exportTitle}>Export data</h2>
          <p className={styles.exportSub}>
            Server-side export of all rows matching your current filters from{' '}
            <strong>{catalog}.{schema}.{table}</strong>.
          </p>
        </div>
      </div>

      <div className={styles.exportMeta}>
        <span>{totalCount != null ? `${totalCount.toLocaleString()} matching rows` : '—'}</span>
        <span>{filterNote}</span>
      </div>

      <div className={styles.exportCards}>
        {formats.map(({ fmt, label, desc, Icon }) => (
          <button
            key={fmt}
            type="button"
            className={styles.exportCard}
            onClick={() => onExport(fmt)}
            disabled={loading}
          >
            <Icon size={22} />
            <strong>{label}</strong>
            <span>{desc}</span>
          </button>
        ))}
      </div>
      {loading && (
        <div className={`${styles.alert} ${styles.alertInfo}`}>
          Export in progress — large tables may take a minute or more.
        </div>
      )}
    </div>
  )
}

/* ── Paste panel ──────────────────────────────────────────────────────────── */
export function PastePanel({ pasteColumns, onApply, onError, onClose }) {
  const [text, setText] = React.useState('')
  const colNames = pasteColumns.map(c => c.column_name).join(', ')

  function apply() {
    const lines = text.trim().split(/\r?\n/).filter(Boolean)
    const delimiter = detectPasteDelimiter(lines, pasteColumns.length)
    const sepLabel = delimiter === '\t' ? 'tab-separated' : 'comma-separated'
    const rows = []
    const skipped = []

    lines.forEach((line, i) => {
      const parts = parseCsvLine(line, delimiter)
      if (parts.length < pasteColumns.length) {
        skipped.push(i + 1)
        return
      }
      const row = {}
      pasteColumns.forEach((col, j) => { row[col.column_name] = (parts[j] || '').trim() })
      rows.push(row)
    })

    if (!rows.length) {
      onError?.(
        skipped.length
          ? `No rows applied — each line needs ${pasteColumns.length} ${sepLabel} values (${colNames}). Check line ${skipped[0]}.`
          : `Paste one row per line with ${pasteColumns.length} ${sepLabel} values.`
      )
      return
    }

    onApply(rows, { skipped: skipped.length })
    setText('')
  }

  return (
    <div className={styles.pasteZone}>
      <div className={styles.pasteInfo}>
        <AlertCircle size={14} className={styles.pasteIcon} />
        <span>
          Paste rows — one row per line, <strong>{pasteColumns.length} values</strong> in this order:&nbsp;
          <strong>{colNames}</strong>
          (comma- or tab-separated; Excel copy/paste uses tabs)
        </span>
      </div>
      <div className={styles.pasteHint}>
        Header row not needed. Audit columns are filled on save.
        Delimiter is detected automatically (comma or tab). Values with commas must be wrapped in double quotes (&quot;hello, world&quot;) or single quotes (&apos;hello, world&apos;).
        Escape quotes inside a value by doubling them: &quot;Say &quot;&quot;hi&quot;&quot;&quot; or &apos;O&apos;&apos;Brien&apos;.
      </div>
      <div className={styles.pasteBody}>
        <textarea
          className={styles.pasteArea}
          value={text}
          onChange={e => setText(e.target.value)}
          placeholder={`CARRIER_011,Test Carrier,COMMERCIAL,NY,ACTIVE,100000,2025-01-01,2026-12-31,Notes here,contact@test.com,/mnt/raw/test,carrier_config.yaml,US-EAST,true`}
          rows={5}
          aria-label="Paste CSV rows"
        />
        <div className={styles.pasteBtns}>
          <button className={`${styles.btn} ${styles.btnPrimary}`} onClick={apply} disabled={!text.trim()}>
            <Check size={13} /> Apply
          </button>
          <button className={`${styles.btn} ${styles.btnOutline}`} onClick={onClose}>
            <X size={13} /> Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

function ComparisonTable({ title, titleClass, row, columns, pkCols, changedCols, allColumns, cellClass }) {
  const pkLower = new Set(pkCols.map(c => c.toLowerCase()))
  return (
    <div className={styles.histCmpBlock}>
      <div className={`${styles.histCmpLabel} ${titleClass}`}>{title}</div>
      <div className={styles.histCmpScroll}>
        <table className={styles.histCmpTable}>
          <thead>
            <tr>
              {columns.map(col => (
                <th key={col}>{columnLabel(allColumns, col)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              {columns.map(col => {
                const isChanged = changedCols.some(c => c.toLowerCase() === col.toLowerCase())
                const isPk = pkLower.has(col.toLowerCase())
                return (
                  <td
                    key={col}
                    className={[
                      isPk ? styles.histCmpPk : '',
                      isChanged ? cellClass : '',
                    ].filter(Boolean).join(' ') || undefined}
                  >
                    {row[col] ?? '(empty)'}
                  </td>
                )
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}

function RecordComparisonCard({ meta, group, columns, cellClassOld, cellClassNew, hidePrevious = false }) {
  return (
    <div className={styles.histCmpCard}>
      {meta && <div className={styles.histCmpMeta}>{meta}</div>}
      {!hidePrevious && (
        <ComparisonTable
          title="Previous"
          titleClass={styles.histCmpLabelPrev}
          row={group.previous}
          columns={group.columns}
          pkCols={group.pkCols}
          changedCols={group.changedCols}
          allColumns={columns}
          cellClass={cellClassOld}
        />
      )}
      <ComparisonTable
        title={hidePrevious ? 'New record' : 'Current'}
        titleClass={styles.histCmpLabelCurr}
        row={group.current}
        columns={group.columns}
        pkCols={group.pkCols}
        changedCols={group.changedCols}
        allColumns={columns}
        cellClass={cellClassNew}
      />
    </div>
  )
}

function ReviewChangeComparison({ changes, columns, pkCols }) {
  const groups = React.useMemo(
    () => groupReviewChangesForComparison(changes, pkCols),
    [changes, pkCols]
  )

  return (
    <div className={styles.histCmpList}>
      {groups.map((group, i) => (
        <RecordComparisonCard
          key={i}
          meta={<span className={styles.histPk}>Record: {group.row_pk}</span>}
          group={group}
          columns={columns}
          cellClassOld={styles.histCmpCellOld}
          cellClassNew={styles.histCmpCellNew}
        />
      ))}
    </div>
  )
}

/* ── Review panel ─────────────────────────────────────────────────────────── */
export function ReviewPanel({ changes, columns = [], pkCols = [], blocking, warnings, loading, onConfirm, onCancel }) {
  return (
    <div className={styles.reviewPanel}>
      <div className={styles.reviewHead}>
        <h3 className={styles.reviewTitle}>Review changes before staging</h3>
        <button className={`${styles.btn} ${styles.btnOutline}`} onClick={onCancel}>
          <X size={13} /> Back
        </button>
      </div>

      {blocking.length > 0 && (
        <div className={`${styles.alert} ${styles.alertError}`}>
          <AlertCircle size={14} />
          <div style={{ flex: 1 }}>
            <strong>Cannot save — fix these errors first:</strong>
            <div className={styles.errorList}>
              {blocking.map((e, i) => (
                <div key={i} className={styles.errorBlock}>
                  {e.column && (
                    <span className={styles.errorCol}>{e.column}</span>
                  )}
                  <span className={styles.errorReason}>
                    {e.reason || e}
                  </span>
                  {e.fix && (
                    <span className={styles.errorFix}>→ {e.fix}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      {warnings.length > 0 && (
        <div className={`${styles.alert} ${styles.alertWarn}`}>
          <AlertCircle size={14} />
          <div style={{ flex: 1 }}>
            <strong>Warnings (save will proceed):</strong>
            <div className={styles.errorList}>
              {warnings.map((w, i) => (
                <div key={i} className={styles.errorBlock}>
                  {w.column && (
                    <span className={styles.errorCol}>{w.column}</span>
                  )}
                  <span className={styles.errorReason} style={{ color: '#92400e' }}>
                    {w.reason || w}
                  </span>
                  {w.fix && (
                    <span className={styles.errorFix} style={{ color: '#0369a1' }}>→ {w.fix}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className={styles.reviewCmpWrap}>
        <ReviewChangeComparison changes={changes} columns={columns} pkCols={pkCols} />
      </div>

      <div className={styles.reviewFoot}>
        <button className={`${styles.btn} ${styles.btnOutline}`} onClick={onCancel}>Cancel</button>
        <button
          className={`${styles.btn} ${styles.btnPrimary}`}
          onClick={onConfirm}
          disabled={blocking.length > 0 || loading}
        >
          <Check size={13} />
          {loading ? 'Staging…' : 'Stage & apply'}
        </button>
      </div>
    </div>
  )
}

/* ── Upload panel ─────────────────────────────────────────────────────────── */
const UPLOAD_MODES = [
  {
    val: 'upsert',
    label: 'Upsert (update + insert)',
    desc: 'Rows with an existing primary key are updated; new primary keys are inserted.',
  },
  {
    val: 'overwrite',
    label: 'Overwrite (truncate)',
    desc: 'Replace the entire table with file contents. Destructive.',
  },
]

function formatFromUploadFile(uploadFile) {
  if (!uploadFile) return 'csv'
  if (uploadFile.format === 'xlsx') return 'excel'
  if (uploadFile.format === 'txt') return 'txt'
  return 'csv'
}

export function UploadPanel({
  schema, table, catalog, uploadFile, onUploadFileChange,
  onBrowse, onUploadSuccess,
}) {
  const [exists, setExists] = React.useState(null)
  const [mode, setMode] = React.useState('upsert')
  const [delimiter, setDelimiter] = React.useState(',')
  const [fileFormat, setFileFormat] = React.useState('csv')
  const [hasHeader, setHasHeader] = React.useState(true)
  const [checking, setChecking] = React.useState(false)
  const [status, setStatus] = React.useState(null)
  const [loading, setLoading] = React.useState(false)
  const [overwriteConfirm, setOverwriteConfirm] = React.useState('')
  const [validation, setValidation] = React.useState(null)
  const [validationErrors, setValidationErrors] = React.useState([])

  React.useEffect(() => {
    if (uploadFile) setFileFormat(formatFromUploadFile(uploadFile))
  }, [uploadFile?.name, uploadFile?.format])

  const dataRowCount = uploadDataRowCount(uploadFile, delimiter, hasHeader)
  const tableExists = exists !== false
  const tableLabel = `${schema}.${table}`
  const preview = uploadPreview(uploadFile, delimiter, hasHeader)

  function resetValidation() {
    setValidation(null)
    setValidationErrors([])
    setStatus(null)
  }

  function onModeChange(next) {
    setMode(next)
    resetValidation()
    setOverwriteConfirm('')
  }

  async function checkTable() {
    if (!schema || !table || !catalog) return
    setChecking(true)
    try {
      const d = await api.uploadCheck(catalog, schema, table)
      setExists(Boolean(d.exists))
    } catch {
      setExists(true)
    } finally {
      setChecking(false)
    }
  }

  React.useEffect(() => {
    setExists(null)
    setMode('upsert')
    resetValidation()
    setOverwriteConfirm('')
    checkTable()
  }, [schema, table, catalog])

  React.useEffect(() => {
    resetValidation()
  }, [uploadFile, delimiter, hasHeader, fileFormat])

  async function doValidate() {
    if (!uploadFile) {
      setStatus({ type: 'error', msg: 'Select a file first.' })
      return
    }
    if (mode === 'overwrite' && tableExists && overwriteConfirm !== tableLabel) {
      setStatus({ type: 'error', msg: `Type "${tableLabel}" to confirm overwrite.` })
      return
    }
    setLoading(true)
    setStatus({
      type: 'info',
      msg: `Validating ${dataRowCount.toLocaleString()} row(s) — this may take a minute for large files…`,
    })
    setValidation(null)
    setValidationErrors([])
    try {
      const result = await api.uploadValidate(schema, table, {
        catalog,
        mode,
        csv_text: uploadFile.csvText || '',
        file_base64: uploadFile.base64 || '',
        file_format: uploadFile.format || fileFormat,
        has_header: hasHeader,
        delimiter,
        filename: uploadFile.name || 'upload.csv',
      })
      setValidation(result)
      if (result.requires_approval) {
        setStatus({
          type: 'success',
          msg: `Validation passed — awaiting approver (${result.change_request_id}).`,
        })
      } else if (mode === 'upsert') {
        const updates = result.summary?.rows_with_changes ?? 0
        const inserts = result.summary?.insert_count ?? 0
        setStatus({
          type: 'success',
          msg: `Validation passed — ${updates} update(s), ${inserts} insert(s).`,
        })
      } else {
        setStatus({
          type: 'success',
          msg: `Validation passed — ${result.summary?.rows_to_insert ?? result.summary?.total_rows ?? 0} row(s) ready to apply.`,
        })
      }
    } catch (e) {
      setValidationErrors(e.detail?.errors || [])
      setStatus({ type: 'error', msg: e.message || 'Validation failed.' })
    } finally {
      setLoading(false)
    }
  }

  async function doApply() {
    if (!validation?.change_request_id) return
    setLoading(true)
    setStatus({ type: 'info', msg: 'Applying changes — please wait…' })
    try {
      const fresh = await api.getChangeRequest(validation.change_request_id)
      if (fresh.status === 'pending_approval') {
        setStatus({ type: 'error', msg: 'Still awaiting approver sign-off. Check the Approvals tab.' })
        setValidation(prev => ({ ...prev, status: fresh.status }))
        return
      }
      const result = await api.uploadApply(schema, table, validation.change_request_id)
      const updated = result.rows_updated ?? 0
      const inserted = result.rows_inserted ?? 0
      let msgText
      if (result.mode === 'upsert') {
        msgText = `Applied — ${updated} updated, ${inserted} inserted`
      } else if (result.mode === 'overwrite') {
        msgText = `Applied — ${inserted || updated || 0} row(s) loaded`
      } else {
        msgText = `Applied — ${updated || inserted} row(s)`
      }
      setStatus({
        type: 'success',
        msg: `${msgText}${result.audit_entries != null ? ` (${result.audit_entries} audit entries)` : ''}.`,
      })
      setValidation(null)
      onUploadFileChange(null)
      setOverwriteConfirm('')
      onUploadSuccess?.()
    } catch (e) {
      setStatus({ type: 'error', msg: e.message || 'Apply failed.' })
    } finally {
      setLoading(false)
    }
  }

  const canApply = validation && (
    validation.can_apply
    || validation.status === 'approved'
    || (validation.status === 'validated' && !validation.requires_approval)
  )

  return (
    <div className={styles.uploadPanel}>
      <div className={styles.uploadHeader}>
        <div>
          <h3 className={styles.uploadTitle}>File upload</h3>
          <p className={styles.uploadSubtitle}>{catalog}.{tableLabel}</p>
        </div>
        <button type="button" className={`${styles.btn} ${styles.btnOutline}`} onClick={onBrowse}>
          <FolderOpen size={14} /> Browse file
        </button>
      </div>

      <section className={styles.uploadSection}>
        <div className={styles.uploadStepLabel}>Step 1 — Upload mode</div>
        <div className={styles.modeCards}>
          {UPLOAD_MODES.map(m => (
            <label
              key={m.val}
              className={`${styles.modeCard} ${mode === m.val ? styles.modeCardActive : ''}`}
            >
              <input type="radio" name="umode" checked={mode === m.val} onChange={() => onModeChange(m.val)} />
              <div>
                <strong>{m.label}</strong>
                <span>{m.desc}</span>
              </div>
            </label>
          ))}
        </div>
      </section>

      <section className={styles.uploadSection}>
        <div className={styles.uploadStepLabel}>Step 2 — File settings</div>
        <div className={styles.uploadSettingsRow}>
          <label className={styles.uploadSetting}>
            Format
            <select value={fileFormat} onChange={e => setFileFormat(e.target.value)}>
              <option value="csv">CSV</option>
              <option value="txt">Text</option>
              <option value="excel">Excel (.xlsx)</option>
            </select>
          </label>
          <label className={styles.uploadSetting}>
            Delimiter
            <select
              value={delimiter}
              onChange={e => setDelimiter(e.target.value)}
              disabled={fileFormat === 'excel'}
            >
              <option value=",">Comma</option>
              <option value="|">Pipe</option>
              <option value="\\t">Tab</option>
              <option value=";">Semicolon</option>
            </select>
          </label>
          <label className={`${styles.uploadSetting} ${styles.uploadCheck}`}>
            <input type="checkbox" checked={hasHeader} onChange={e => setHasHeader(e.target.checked)} />
            Header row
          </label>
        </div>
        <p className={styles.fieldHint}>
          {uploadFile
            ? `${uploadFile.name || 'File'} — ${dataRowCount} data row${dataRowCount === 1 ? '' : 's'}`
            : 'No file selected.'}
        </p>
      </section>

      {checking && <div className={`${styles.alert} ${styles.alertInfo}`}>Checking table…</div>}
      {exists === false && mode === 'overwrite' && (
        <div className={`${styles.alert} ${styles.alertInfo}`}>
          <Check size={14} /> Table will be created on first append upload.
        </div>
      )}

      {uploadFile && preview.headers.length > 0 && (
        <section className={styles.uploadSection}>
          <div className={styles.uploadStepLabel}>Step 3 — Preview</div>
          <div className={styles.uploadPreviewWrap}>
            <table className={styles.uploadPreviewTable}>
              <thead>
                <tr>{preview.headers.map(h => <th key={h}>{h}</th>)}</tr>
              </thead>
              <tbody>
                {preview.rows.map((row, i) => (
                  <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {mode === 'overwrite' && tableExists && (
        <div className={`${styles.alert} ${styles.alertError}`}>
          <AlertCircle size={14} />
          <div>
            <strong>Destructive action</strong> — all existing rows will be deleted.
            <label className={styles.overwriteConfirm}>
              Type <code>{tableLabel}</code> to confirm
              <input
                value={overwriteConfirm}
                onChange={e => setOverwriteConfirm(e.target.value)}
                placeholder={tableLabel}
              />
            </label>
          </div>
        </div>
      )}

      {validation && (
        <div className={`${styles.alert} ${styles.alertInfo}`}>
          <Check size={14} />
          <div>
            <strong>
              {validation.requires_approval && validation.status === 'pending_approval'
                ? 'Awaiting approval'
                : 'Ready to apply'}
            </strong> ({validation.change_request_id})
            <ul className={styles.summaryList}>
              <li>Mode: {validation.mode}</li>
              <li>Total rows: {validation.summary?.total_rows}</li>
              {validation.mode === 'upsert' && (
                <>
                  <li>Rows to update: {validation.summary?.rows_with_changes}</li>
                  <li>Rows to insert: {validation.summary?.insert_count}</li>
                  <li>Unchanged: {validation.summary?.rows_unchanged}</li>
                </>
              )}
              {validation.mode === 'update' && (
                <>
                  <li>Rows changing: {validation.summary?.rows_with_changes}</li>
                  <li>Unchanged: {validation.summary?.rows_unchanged}</li>
                </>
              )}
              {(validation.mode === 'append' || validation.mode === 'overwrite') && (
                <>
                  <li>Rows to insert: {validation.summary?.rows_to_insert}</li>
                  {validation.summary?.will_truncate && <li>Will truncate existing data</li>}
                </>
              )}
              {validation.summary?.columns_changing?.length > 0 && (
                <li>Columns: {validation.summary.columns_changing.join(', ')}</li>
              )}
              {validation.review_url && (
                <li>Approval link: <code>{validation.review_url}</code></li>
              )}
            </ul>
          </div>
        </div>
      )}

      {validationErrors.length > 0 && (
        <div className={`${styles.alert} ${styles.alertError}`}>
          <AlertCircle size={14} />
          <div>
            <strong>{validationErrors.length} validation error(s)</strong>
            <ul className={styles.errorListCompact}>
              {validationErrors.slice(0, 8).map((e, i) => (
                <li key={i}>
                  {Array.isArray(e.duplicate_rows) && e.duplicate_rows.length > 0 ? (
                    <>
                      <strong>Rows {e.duplicate_rows.join(', ')}</strong>
                      {e.pk && typeof e.pk === 'object' && Object.keys(e.pk).length > 0 && (
                        <>
                          {' '}
                          (
                          {Object.entries(e.pk)
                            .map(([k, v]) => `${k}=${v || '(blank)'}`)
                            .join(', ')}
                          )
                        </>
                      )}
                      : {e.reason}
                    </>
                  ) : (
                    <>Row {e.row}: {e.reason}</>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {status && (
        <div className={`${styles.alert} ${status.type === 'error' ? styles.alertError : styles.alertInfo}`}>
          {status.msg}
        </div>
      )}

      <div className={styles.uploadActions}>
        <button
          type="button"
          className={`${styles.btn} ${styles.btnOutline}`}
          onClick={doValidate}
          disabled={loading || !uploadFile}
        >
          {loading && !validation ? 'Validating…' : 'Validate'}
        </button>
        <button
          type="button"
          className={`${styles.btn} ${styles.btnPrimary}`}
          onClick={doApply}
          disabled={loading || !canApply}
        >
          {loading && validation ? 'Applying…' : mode === 'overwrite' ? 'Apply overwrite' : 'Apply changes'}
        </button>
      </div>
    </div>
  )
}

/* ── Approver diff view ───────────────────────────────────────────────────── */
const APPROVAL_DIFF_PAGE_SIZE = 25

function ApprovalDiffView({
  diffs = [],
  pkCols = [],
  businessKeyCols = [],
  columns = [],
}) {
  const [page, setPage] = React.useState(0)

  const groups = React.useMemo(
    () => groupApprovalDiffsForComparison(diffs, { pkCols, businessKeyCols }),
    [diffs, pkCols, businessKeyCols],
  )

  React.useEffect(() => {
    setPage(0)
  }, [diffs, pkCols, businessKeyCols])

  if (!groups.length) {
    return <div className={styles.emptyHistory}>No changes to review.</div>
  }

  const fieldChanges = (diffs || []).length
  const totalPages = Math.max(1, Math.ceil(groups.length / APPROVAL_DIFF_PAGE_SIZE))
  const pageGroups = groups.slice(
    page * APPROVAL_DIFF_PAGE_SIZE,
    (page + 1) * APPROVAL_DIFF_PAGE_SIZE,
  )
  const inserts = groups.filter(g => g.operation === 'insert').length
  const updates = groups.length - inserts

  return (
    <div className={styles.approvalDiffWrap}>
      <div className={styles.approvalDiffSummary}>
        <span>
          <strong>{groups.length}</strong> record{groups.length === 1 ? '' : 's'}
          {updates > 0 && inserts > 0
            ? ` (${updates} update${updates === 1 ? '' : 's'}, ${inserts} new)`
            : inserts > 0
              ? ' (all new)'
              : ''}
          {' · '}
          <strong>{fieldChanges}</strong> field change{fieldChanges === 1 ? '' : 's'}
        </span>
        {groups.length > APPROVAL_DIFF_PAGE_SIZE && (
          <span className={styles.approvalDiffPaging}>
            Showing {page * APPROVAL_DIFF_PAGE_SIZE + 1}–
            {Math.min((page + 1) * APPROVAL_DIFF_PAGE_SIZE, groups.length)} of {groups.length}
            {' · '}
            <button
              type="button"
              className={styles.approvalDiffPageBtn}
              disabled={page <= 0}
              onClick={() => setPage(p => Math.max(0, p - 1))}
            >
              Previous
            </button>
            {' '}
            <button
              type="button"
              className={styles.approvalDiffPageBtn}
              disabled={page >= totalPages - 1}
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
            >
              Next
            </button>
          </span>
        )}
      </div>
      <div className={styles.histCmpList}>
        {pageGroups.map((group, i) => (
          <RecordComparisonCard
            key={`${group.recordLabel}-${page}-${i}`}
            meta={(
              <span className={styles.histPk}>
                Record: {group.recordLabel}
                {group.operation === 'insert' && (
                  <span className={styles.approvalOpBadge}>New</span>
                )}
                {group.operation === 'update' && group.changedCols.length > 0 && (
                  <span className={styles.approvalOpBadgeMuted}>
                    {group.changedCols.length} field{group.changedCols.length === 1 ? '' : 's'}
                  </span>
                )}
              </span>
            )}
            group={group}
            columns={columns}
            cellClassOld={styles.histCmpCellOld}
            cellClassNew={styles.histCmpCellNew}
            hidePrevious={group.operation === 'insert'}
          />
        ))}
      </div>
    </div>
  )
}

/* ── Approvals panel (Phase 6) ─────────────────────────────────────────────── */
const APPROVAL_INBOX_PAGE_SIZE = 20
const APPROVAL_CACHE_MAX = 5
const APPROVAL_QUICK_PREVIEW_MAX_ROWS = 25

function limitCacheSize(cache, maxSize) {
  const next = { ...cache }
  const keys = Object.keys(next)
  while (keys.length > maxSize) {
    delete next[keys[0]]
    keys.shift()
  }
  return next
}

function formatApprovalSubmittedAt(value) {
  const text = String(value || '').replace('T', ' ').substring(0, 16)
  return text || '—'
}

function ApprovalSqlReview({ reviewSql, onCopyStatus }) {
  const compare = reviewSql?.compare_sql
    || reviewSql?.queries?.find(q => q.id === 'compare')?.sql
    || reviewSql?.queries?.[0]?.sql

  if (!compare) {
    return <div className={styles.emptyHistory}>No compare SQL available for this request.</div>
  }

  async function copySql(sql) {
    try {
      await navigator.clipboard.writeText(sql)
      onCopyStatus?.('SQL copied — paste into Databricks SQL and run against your warehouse.')
    } catch {
      onCopyStatus?.('Could not copy to clipboard.', 'error')
    }
  }

  const editorUrl = reviewSql?.databricks_sql_editor_url

  return (
    <div className={styles.approvalSqlWrap}>
      <p className={styles.approvalSqlIntro}>
        Run this compare query in Databricks SQL (primary review path). It joins staged rows to the live table
        for this change request — PK plus changed columns only.
        {reviewSql?.logged_at && (
          <> Query logged in <code>dmz.dataeditor_approval_review_sql</code>.</>
        )}
      </p>
      <div className={styles.approvalSqlBlock}>
        <div className={styles.approvalSqlHead}>
          <div>
            <strong>Staged vs live compare</strong>
            <div className={styles.approvalSqlDesc}>
              {reviewSql?.staging_table} → {reviewSql?.target_table}
            </div>
          </div>
          <div className={styles.approvalSqlActions}>
            {editorUrl && (
              <a
                href={editorUrl}
                target="_blank"
                rel="noopener noreferrer"
                className={`${styles.btn} ${styles.btnOutline}`}
              >
                Open Databricks SQL
              </a>
            )}
            <button type="button" className={`${styles.btn} ${styles.btnPrimary}`} onClick={() => copySql(compare)}>
              Copy SQL
            </button>
          </div>
        </div>
        <pre className={styles.approvalSqlPre}>{compare}</pre>
      </div>
    </div>
  )
}

export function ApprovalsPanel({ tokenReview, onClearToken, onApplied }) {
  const [pending, setPending] = React.useState([])
  const [inboxTotal, setInboxTotal] = React.useState(0)
  const [inboxPage, setInboxPage] = React.useState(1)
  const [tableOptions, setTableOptions] = React.useState([])
  const [tableFilter, setTableFilter] = React.useState('')
  const [loading, setLoading] = React.useState(false)
  const [status, setStatus] = React.useState(null)
  const [rejectId, setRejectId] = React.useState(null)
  const [rejectReason, setRejectReason] = React.useState('')
  const [review, setReview] = React.useState(tokenReview || null)
  const [expandedId, setExpandedId] = React.useState(null)
  const [expandMode, setExpandMode] = React.useState('sql')
  const [diffCache, setDiffCache] = React.useState({})
  const [sqlCache, setSqlCache] = React.useState({})
  const [detailLoading, setDetailLoading] = React.useState(null)
  const [detailsOpenId, setDetailsOpenId] = React.useState(null)

  async function loadPending(page = inboxPage, filter = tableFilter) {
    setLoading(true)
    try {
      let schemaName = ''
      let tableName = ''
      if (filter) {
        const [schema, table] = filter.split('\0')
        schemaName = schema || ''
        tableName = table || ''
      }
      const result = await api.listPendingChangeRequests({
        page,
        pageSize: APPROVAL_INBOX_PAGE_SIZE,
        schemaName,
        tableName,
      })
      setPending(Array.isArray(result?.items) ? result.items : [])
      setInboxTotal(Number(result?.total) || 0)
      setInboxPage(Number(result?.page) || page)
      setTableOptions(Array.isArray(result?.tables) ? result.tables : [])
    } catch (e) {
      setStatus({ type: 'error', msg: e.message || 'Could not load pending approvals.' })
    } finally {
      setLoading(false)
    }
  }

  React.useEffect(() => {
    loadPending(1, tableFilter)
  }, [tableFilter])

  React.useEffect(() => {
    setReview(tokenReview || null)
  }, [tokenReview])

  async function applyApprovedChange(rec) {
    const id = rec.change_request_id
    const schemaName = rec.schema_name
    const tableName = rec.table_name
    const requestType = rec.request_type || 'upload'
    if (requestType === 'grid_edit') {
      return api.applyGridEdits(schemaName, tableName, id)
    }
    return api.uploadApply(schemaName, tableName, id)
  }

  async function doApprove(id, rec, { applyAfter = false } = {}) {
    setLoading(true)
    try {
      await api.approveChangeRequest(id)
      if (applyAfter) {
        const result = await applyApprovedChange(rec)
        const updated = result.rows_updated ?? 0
        const inserted = result.rows_inserted ?? 0
        setStatus({
          type: 'success',
          msg: `Approved and applied ${id} — ${updated} updated, ${inserted} inserted.`,
        })
      } else {
        setStatus({ type: 'success', msg: `Approved ${id}. Use "Apply to table" to merge staged data.` })
      }
      await loadPending(inboxPage, tableFilter)
      onApplied?.()
    } catch (e) {
      setStatus({ type: 'error', msg: e.message || (applyAfter ? 'Approve & apply failed.' : 'Approve failed.') })
    } finally {
      setLoading(false)
    }
  }

  async function doReject(id) {
    setLoading(true)
    try {
      await api.rejectChangeRequest(id, rejectReason)
      setStatus({ type: 'success', msg: `Rejected ${id}.` })
      setRejectId(null)
      setRejectReason('')
      await loadPending(inboxPage, tableFilter)
    } catch (e) {
      setStatus({ type: 'error', msg: e.message || 'Reject failed.' })
    } finally {
      setLoading(false)
    }
  }

  function parseSummary(rec) {
    try {
      return typeof rec.change_summary === 'string'
        ? JSON.parse(rec.change_summary)
        : (rec.change_summary || {})
    } catch {
      return {}
    }
  }

  async function loadReviewDetail(id, mode = 'sql') {
    if (expandedId === id && expandMode === mode) {
      setExpandedId(null)
      return
    }
    setExpandedId(id)
    setExpandMode(mode)
    if (mode === 'sql') {
      if (sqlCache[id]) return
      setDetailLoading(id)
      try {
        const data = await api.getChangeRequestReviewSql(id)
        setSqlCache(prev => limitCacheSize({ ...prev, [id]: data }, APPROVAL_CACHE_MAX))
      } catch (e) {
        setStatus({ type: 'error', msg: e.message || 'Could not load review SQL.' })
      } finally {
        setDetailLoading(null)
      }
      return
    }
    if (diffCache[id]) return
    setDetailLoading(id)
    try {
      const data = await api.getChangeRequestDiffs(id)
      setDiffCache(prev => limitCacheSize({ ...prev, [id]: data }, APPROVAL_CACHE_MAX))
    } catch (e) {
      setStatus({ type: 'error', msg: e.message || 'Could not load preview.' })
    } finally {
      setDetailLoading(null)
    }
  }

  function resolveDiffBundle(id, summary) {
    const cached = diffCache[id]
    if (cached && !Array.isArray(cached)) {
      return {
        diffs: cached.diffs || [],
        pkCols: cached.pk_cols || [],
        businessKeyCols: cached.business_key_cols || cached.pk_cols || [],
      }
    }
    if (Array.isArray(cached)) {
      return { diffs: cached, pkCols: [], businessKeyCols: [] }
    }
    return {
      diffs: summary.all_diffs || summary.sample_diffs || [],
      pkCols: [],
      businessKeyCols: [],
    }
  }

  React.useEffect(() => {
    if (tokenReview?.change_request_id && tokenReview.diffs) {
      setDiffCache(prev => limitCacheSize({
        ...prev,
        [tokenReview.change_request_id]: {
          diffs: tokenReview.diffs,
          pk_cols: tokenReview.pk_cols || [],
          business_key_cols: tokenReview.business_key_cols || tokenReview.pk_cols || [],
        },
      }, APPROVAL_CACHE_MAX))
      setExpandedId(tokenReview.change_request_id)
      setExpandMode('preview')
    }
  }, [tokenReview])

  const inboxTotalPages = Math.max(1, Math.ceil(inboxTotal / APPROVAL_INBOX_PAGE_SIZE))

  return (
    <div className={styles.uploadPanel}>
      <div className={styles.uploadHeader}>
        <div>
          <h3 className={styles.uploadTitle}>Approvals</h3>
          <p className={styles.uploadSubtitle}>
            Review staged changes in Databricks SQL, then approve and apply to the live table.
          </p>
        </div>
        <button
          type="button"
          className={`${styles.btn} ${styles.btnOutline}`}
          onClick={() => loadPending(inboxPage, tableFilter)}
          disabled={loading}
        >
          Refresh
        </button>
      </div>

      <div className={styles.approvalInboxToolbar}>
        <label className={styles.approvalFilterLabel}>
          Table
          <select
            value={tableFilter}
            onChange={e => { setTableFilter(e.target.value); setInboxPage(1) }}
          >
            <option value="">All tables ({tableOptions.reduce((n, t) => n + (t.pending_count || 0), 0) || inboxTotal})</option>
            {tableOptions.map(t => (
              <option key={`${t.schema_name}\0${t.table_name}`} value={`${t.schema_name}\0${t.table_name}`}>
                {t.display_name || t.table_name} ({t.pending_count})
              </option>
            ))}
          </select>
        </label>
        {inboxTotal > 0 && (
          <span className={styles.approvalInboxCount}>
            {inboxTotal} pending
            {inboxTotalPages > 1 && ` · page ${inboxPage} of ${inboxTotalPages}`}
          </span>
        )}
      </div>

      {review && (
        <div className={`${styles.alert} ${styles.alertInfo}`}>
          <strong>Approval link</strong> — {review.change_request_id} ({review.schema_name}.{review.table_name}, {review.mode})
          {onClearToken && (
            <button type="button" className={`${styles.btn} ${styles.btnOutline}`} style={{ marginLeft: 8 }} onClick={onClearToken}>
              Dismiss
            </button>
          )}
        </div>
      )}

      {status && (
        <div className={`${styles.alert} ${status.type === 'error' ? styles.alertError : styles.alertInfo}`}>
          {status.msg}
        </div>
      )}

      {loading && pending.length === 0 && <div className={`${styles.alert} ${styles.alertInfo}`}>Loading…</div>}
      {!loading && pending.length === 0 && (
        <div className={`${styles.alert} ${styles.alertInfo}`}>No uploads awaiting your approval.</div>
      )}

      {pending.map(rec => {
        const summary = parseSummary(rec)
        const id = rec.change_request_id
        const rowCount = rec.row_count ?? summary.total_rows ?? '—'
        const displayName = rec.display_name || rec.table_name
        const fieldChanges = summary.all_diffs?.length ?? summary.sample_diffs?.length
        const numericRowCount = Number(rowCount)
        const quickPreviewEligible = (
          sqlCache[id]?.quick_preview_eligible
          ?? (!Number.isNaN(numericRowCount) && numericRowCount <= APPROVAL_QUICK_PREVIEW_MAX_ROWS)
        )
        return (
          <section key={id} className={styles.uploadSection}>
            <div className={styles.approvalCardHead}>
              <div>
                <div className={styles.approvalCardTitle}>
                  {displayName}
                  <span className={styles.approvalCardMetaSep}>·</span>
                  {rec.submitted_by || 'unknown'}
                  <span className={styles.approvalCardMetaSep}>·</span>
                  {rowCount} row{rowCount === 1 ? '' : 's'}
                </div>
                <div className={styles.approvalCardSub}>
                  {rec.request_type === 'grid_edit' ? 'Grid edit' : 'Upload'}
                  {rec.mode ? ` · ${rec.mode}` : ''}
                  <span className={styles.approvalCardMetaSep}>·</span>
                  {formatApprovalSubmittedAt(rec.submitted_at)}
                  {fieldChanges != null && (
                    <>
                      <span className={styles.approvalCardMetaSep}>·</span>
                      {fieldChanges} field change{fieldChanges === 1 ? '' : 's'}
                    </>
                  )}
                  {summary.insert_count > 0 && (
                    <>
                      <span className={styles.approvalCardMetaSep}>·</span>
                      {summary.insert_count} new
                    </>
                  )}
                </div>
              </div>
              <button
                type="button"
                className={styles.approvalDetailsToggle}
                onClick={() => setDetailsOpenId(detailsOpenId === id ? null : id)}
              >
                {detailsOpenId === id ? 'Hide details' : 'Details'}
              </button>
            </div>
            {detailsOpenId === id && (
              <div className={styles.approvalTechnical}>
                Request {id} · {rec.schema_name}.{rec.table_name}
                {rec.staging_table_name && <> · stage: {rec.staging_table_name}</>}
              </div>
            )}
            <div className={styles.uploadActions}>
              <button
                type="button"
                className={`${styles.btn} ${styles.btnOutline}`}
                onClick={() => loadReviewDetail(id, 'sql')}
                disabled={detailLoading === id}
              >
                {detailLoading === id && expandMode === 'sql' ? 'Loading…' : expandedId === id && expandMode === 'sql' ? 'Hide SQL' : 'Review SQL'}
              </button>
              {quickPreviewEligible && (
                <button
                  type="button"
                  className={`${styles.btn} ${styles.btnOutline}`}
                  onClick={() => loadReviewDetail(id, 'preview')}
                  disabled={detailLoading === id}
                >
                  {expandedId === id && expandMode === 'preview' ? 'Hide preview' : 'Quick preview'}
                </button>
              )}
              <button
                type="button"
                className={`${styles.btn} ${styles.btnPrimary}`}
                onClick={() => doApprove(id, rec, { applyAfter: true })}
                disabled={loading}
              >
                Approve &amp; apply
              </button>
              <button type="button" className={`${styles.btn} ${styles.btnOutline}`} onClick={() => setRejectId(id)} disabled={loading}>
                Reject
              </button>
            </div>
            {expandedId === id && expandMode === 'sql' && (
              <div className={styles.reviewCmpWrap} style={{ marginTop: 12 }}>
                <ApprovalSqlReview
                  reviewSql={sqlCache[id]}
                  onCopyStatus={(msg, type = 'info') => setStatus({ type, msg })}
                />
              </div>
            )}
            {expandedId === id && expandMode === 'preview' && (() => {
              const bundle = resolveDiffBundle(id, summary)
              return (
              <div className={styles.reviewCmpWrap} style={{ marginTop: 12 }}>
                <ApprovalDiffView
                  diffs={bundle.diffs}
                  pkCols={bundle.pkCols}
                  businessKeyCols={bundle.businessKeyCols}
                />
              </div>
              )
            })()}
            {rejectId === id && (
              <label className={styles.overwriteConfirm}>
                Rejection reason
                <input value={rejectReason} onChange={e => setRejectReason(e.target.value)} placeholder="Optional reason" />
                <button type="button" className={`${styles.btn} ${styles.btnPrimary}`} onClick={() => doReject(id)} disabled={loading}>
                  Confirm reject
                </button>
              </label>
            )}
          </section>
        )
      })}

      {inboxTotalPages > 1 && (
        <div className={styles.approvalInboxPager}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnOutline}`}
            disabled={loading || inboxPage <= 1}
            onClick={() => loadPending(inboxPage - 1, tableFilter)}
          >
            Previous page
          </button>
          <span>Page {inboxPage} of {inboxTotalPages}</span>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnOutline}`}
            disabled={loading || inboxPage >= inboxTotalPages}
            onClick={() => loadPending(inboxPage + 1, tableFilter)}
          >
            Next page
          </button>
        </div>
      )}
    </div>
  )
}

/* ── History panel ────────────────────────────────────────────────────────── */
const HISTORY_VIEWS = [
  { id: 'log', label: 'Changes log' },
  { id: 'comparison', label: 'Change comparison' },
]

function HistoryChangesLog({ history }) {
  return (
    <table className={styles.histTable}>
      <thead>
        <tr>
          <th>Changed at</th>
          <th>Changed by</th>
          <th>Record key</th>
          <th>Column</th>
          <th>Old value</th>
          <th>New value</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {history.map((h, i) => (
          <tr key={i}>
            <td><span className={styles.histDate}>{formatHistoryTimestamp(h.changed_at)}</span></td>
            <td>{h.changed_by}</td>
            <td><span className={styles.histPk}>{h.record_key}</span></td>
            <td>{h.column_name}</td>
            <td><span className={styles.histOld}>{h.old_value}</span></td>
            <td><span className={styles.histNew}>{h.new_value}</span></td>
            <td><span className={styles.histSource}>{h.change_source}</span></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function HistoryChangeComparison({ history, columns }) {
  const groups = React.useMemo(() => groupHistoryForComparison(history), [history])

  return (
    <div className={styles.histCmpList}>
      {groups.map((group, i) => (
        <RecordComparisonCard
          key={i}
          meta={(
            <>
              <span className={styles.histCmpMetaDate}>{formatHistoryTimestamp(group.changed_at)}</span>
              <span className={styles.histCmpMetaSep}>·</span>
              <span>{group.changed_by}</span>
              <span className={styles.histCmpMetaSep}>·</span>
              <span className={styles.histCmpMetaSource}>{group.change_source}</span>
              <span className={styles.histCmpMetaSep}>·</span>
              <span className={styles.histPk}>{group.record_key}</span>
            </>
          )}
          group={group}
          columns={columns}
          cellClassOld={styles.histCmpCellOld}
          cellClassNew={styles.histCmpCellNew}
        />
      ))}
    </div>
  )
}

export function HistoryPanel({ history, columns = [] }) {
  const [view, setView] = React.useState('log')

  if (!history.length) {
    return <div className={styles.emptyHistory}>No change history recorded for this table.</div>
  }

  return (
    <div className={styles.histPanel}>
      <div className={styles.histSubTabs} role="tablist" aria-label="History views">
        {HISTORY_VIEWS.map(tab => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={view === tab.id}
            className={`${styles.histSubTab} ${view === tab.id ? styles.histSubTabActive : ''}`}
            onClick={() => setView(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className={styles.histWrap} role="tabpanel">
        {view === 'log' ? (
          <HistoryChangesLog history={history} />
        ) : (
          <HistoryChangeComparison history={history} columns={columns} />
        )}
      </div>
    </div>
  )
}

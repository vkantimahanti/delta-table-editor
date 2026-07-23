import React from 'react'
import {
  ShieldCheck, Table2, Clock, Database, ArrowRight, RefreshCw,
  FileEdit, Upload,
} from 'lucide-react'
import { api } from '../api/client.js'
import { formatHistoryTimestamp } from '../utils/historyGroup.js'
import styles from './Overview.module.css'

function statusLabel(status) {
  const map = {
    pending_approval: 'Awaiting approval',
    validated: 'Validated (staged)',
    approved: 'Approved',
    applied: 'Applied',
    rejected: 'Rejected',
  }
  return map[status] || status
}

function MetricCard({ icon: Icon, label, value, accent }) {
  return (
    <div className={`${styles.metricCard} ${accent ? styles.metricAccent : ''}`}>
      <div className={styles.metricIcon}><Icon size={18} /></div>
      <div>
        <div className={styles.metricLabel}>{label}</div>
        <div className={styles.metricValue}>{value}</div>
      </div>
    </div>
  )
}

export default function Overview({
  onGoToApprovals,
  onGoToDataEditor,
  onOpenTable,
  onPendingCountChange,
}) {
  const [data, setData] = React.useState(null)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState(null)

  async function load(forceRefresh = false) {
    setLoading(true)
    setError(null)
    try {
      const result = await api.getOverview(forceRefresh)
      setData(result)
      onPendingCountChange?.(result?.metrics?.pending_approvals ?? 0)
    } catch (e) {
      setError(e.message || 'Could not load overview.')
    } finally {
      setLoading(false)
    }
  }

  React.useEffect(() => { load() }, [])

  const metrics = data?.metrics || {}
  const pending = data?.pending_approvals || []
  const recentEdits = data?.recent_edits || []
  const recentRequests = data?.recent_requests || []

  return (
    <div className={styles.overview}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>Overview</h1>
          <p className={styles.subtitle}>
            Pending approvals, staged changes, and recent edits across your governed tables.
          </p>
        </div>
        <button type="button" className={styles.refreshBtn} onClick={() => load(true)} disabled={loading}>
          <RefreshCw size={14} className={loading ? styles.spin : ''} />
          Refresh
        </button>
      </header>

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.metrics}>
        <MetricCard
          icon={ShieldCheck}
          label="Pending approvals"
          value={loading ? '—' : metrics.pending_approvals ?? 0}
          accent={metrics.pending_approvals > 0}
        />
        <MetricCard
          icon={Database}
          label="Registered tables"
          value={loading ? '—' : metrics.registered_tables ?? 0}
        />
        <MetricCard
          icon={Upload}
          label="Staged requests"
          value={loading ? '—' : metrics.staged_requests ?? 0}
        />
        <MetricCard
          icon={FileEdit}
          label="Edits (last 24h)"
          value={loading ? '—' : metrics.edits_last_24h ?? 0}
        />
      </div>

      <div className={styles.grid}>
        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Pending approvals</h2>
            <button type="button" className={styles.linkBtn} onClick={onGoToApprovals}>
              Review all <ArrowRight size={14} />
            </button>
          </div>
          <p className={styles.panelHint}>
            Staged uploads awaiting validation and approval before apply.
          </p>
          {loading && pending.length === 0 && <div className={styles.empty}>Loading…</div>}
          {!loading && pending.length === 0 && (
            <div className={styles.empty}>No uploads awaiting your approval.</div>
          )}
          <ul className={styles.list}>
            {pending.slice(0, 5).map(rec => (
              <li key={rec.change_request_id} className={styles.listItem}>
                <div>
                  <strong>{rec.schema_name}.{rec.table_name}</strong>
                  <span className={styles.meta}>
                    {rec.mode} · {rec.change_request_id} · {rec.submitted_by}
                  </span>
                </div>
                <button type="button" className={styles.smallBtn} onClick={onGoToApprovals}>
                  Review
                </button>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.panel}>
          <div className={styles.panelHead}>
            <h2>Recent staged activity</h2>
            <button type="button" className={styles.linkBtn} onClick={onGoToDataEditor}>
              Data Editor <ArrowRight size={14} />
            </button>
          </div>
          <p className={styles.panelHint}>
            Validate → approve → apply workflow for bulk changes.
          </p>
          {loading && recentRequests.length === 0 && <div className={styles.empty}>Loading…</div>}
          {!loading && recentRequests.length === 0 && (
            <div className={styles.empty}>No recent change requests.</div>
          )}
          <ul className={styles.list}>
            {recentRequests.map(rec => (
              <li key={rec.change_request_id} className={styles.listItem}>
                <div>
                  <strong>{rec.schema_name}.{rec.table_name}</strong>
                  <span className={styles.meta}>
                    {statusLabel(rec.status)} · {rec.request_type}
                    {rec.row_count != null ? ` · ${rec.row_count} rows` : ''}
                  </span>
                </div>
                <span className={styles.badge}>{rec.mode || rec.request_type}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <h2>Recent edits</h2>
          <button type="button" className={styles.linkBtn} onClick={onGoToDataEditor}>
            Open Data Editor <ArrowRight size={14} />
          </button>
        </div>
        <p className={styles.panelHint}>
          Latest column-level changes applied across all tables.
        </p>
        {loading && recentEdits.length === 0 && <div className={styles.empty}>Loading…</div>}
        {!loading && recentEdits.length === 0 && (
          <div className={styles.empty}>No edit history recorded yet.</div>
        )}
        {recentEdits.length > 0 && (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Table</th>
                  <th>Changed by</th>
                  <th>Record</th>
                  <th>Column</th>
                  <th>Source</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {recentEdits.map((row, i) => (
                  <tr key={`${row.changed_at}-${row.record_key}-${row.column_name}-${i}`}>
                    <td><span className={styles.time}><Clock size={12} /> {formatHistoryTimestamp(row.changed_at)}</span></td>
                    <td>{row.table_schema}.{row.table_name}</td>
                    <td>{row.changed_by}</td>
                    <td className={styles.mono}>{row.record_key}</td>
                    <td>{row.column_name}</td>
                    <td>{row.change_source}</td>
                    <td>
                      {onOpenTable && (
                        <button
                          type="button"
                          className={styles.smallBtn}
                          onClick={() => onOpenTable(row.table_schema, row.table_name)}
                        >
                          Open
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className={styles.quickActions}>
        <button type="button" className={styles.primaryAction} onClick={onGoToDataEditor}>
          <Table2 size={16} /> Go to Data Editor
        </button>
        <button type="button" className={styles.secondaryAction} onClick={onGoToApprovals}>
          <ShieldCheck size={16} /> Review approvals
        </button>
      </div>
    </div>
  )
}

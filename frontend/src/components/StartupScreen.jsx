import React from 'react'
import { Database } from 'lucide-react'
import { formatElapsed } from '../hooks/useWarehouseStartup.js'
import styles from './StartupScreen.module.css'

export default function StartupScreen({ elapsed, statusMessage, showSlowWarning, onRetry }) {
  return (
    <div className={styles.screen} role="status" aria-live="polite" aria-busy="true">
      <div className={styles.card}>
        <div className={styles.iconWrap} aria-hidden="true">
          <Database className={styles.icon} size={48} strokeWidth={1.5} />
        </div>

        <h1 className={styles.heading}>Starting Delta Table Editor</h1>

        <p className={styles.subtext}>
          Your data warehouse cluster is spinning up and this usually takes 3–5 minutes on first use today.
          Subsequent loads will be instant.
        </p>

        <p className={styles.status}>{statusMessage}</p>
        <p className={styles.elapsed}>{formatElapsed(elapsed)}</p>

        {showSlowWarning && (
          <div className={styles.slowBlock}>
            <p className={styles.slowText}>
              Taking longer than usual. You can keep waiting, or refresh the page to retry.
            </p>
            <button type="button" className={styles.retryBtn} onClick={onRetry}>
              Retry now
            </button>
          </div>
        )}

        <p className={styles.tip}>
          💡 Once loaded, this app will stay fast for the rest of your session.
        </p>
      </div>
    </div>
  )
}

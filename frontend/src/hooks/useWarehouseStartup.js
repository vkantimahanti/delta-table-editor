import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api/client.js'

export const WAREHOUSE_SESSION_KEY = 'data-canvas-warehouse-ready'
const POLL_MS = 5000
const SLOW_SEC = 360
const ROTATE_SEC = 18

const STATUS_PHASES = [
  {
    until: 30,
    messages: [
      'Connecting to Databricks...',
      'Initializing app services...',
    ],
  },
  {
    until: 90,
    messages: [
      'Waking up SQL warehouse...',
      'Starting SQL warehouse cluster...',
    ],
  },
  {
    until: 180,
    messages: [
      'Warehouse is starting — almost there...',
      'Loading warehouse metadata...',
    ],
  },
  {
    until: Infinity,
    messages: [
      'Still starting — this can take up to 5 minutes on a cold start...',
      'Please wait while compute spins up...',
    ],
  },
]

export function formatElapsed(seconds) {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `Elapsed: ${m}m ${s}s`
}

export function getStatusMessage(elapsedSec) {
  const phase = STATUS_PHASES.find(p => elapsedSec < p.until) ?? STATUS_PHASES[STATUS_PHASES.length - 1]
  const idx = Math.floor(elapsedSec / ROTATE_SEC) % phase.messages.length
  return phase.messages[idx]
}

export function useWarehouseStartup() {
  const cached = sessionStorage.getItem(WAREHOUSE_SESSION_KEY) === 'true'
  const [warehouseReady, setWarehouseReady] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef(Date.now())

  const markReady = useCallback(() => {
    sessionStorage.setItem(WAREHOUSE_SESSION_KEY, 'true')
    setWarehouseReady(true)
  }, [])

  const markNotReady = useCallback(() => {
    sessionStorage.removeItem(WAREHOUSE_SESSION_KEY)
    setWarehouseReady(false)
  }, [])

  const checkHealth = useCallback(async () => {
    try {
      const health = await api.health()
      if (health.db === 'ok') {
        markReady()
        return true
      }
      markNotReady()
    } catch (_) {
      markNotReady()
    }
    return false
  }, [markReady, markNotReady])

  const retryNow = useCallback(async () => {
    startRef.current = Date.now()
    setElapsed(0)
    await checkHealth()
  }, [checkHealth])

  useEffect(() => {
    if (warehouseReady) return undefined

    if (cached) {
      // Re-verify auth/SQL even when session cache says ready (scopes may have changed).
      checkHealth()
    } else {
      checkHealth()
    }
    const pollId = setInterval(checkHealth, POLL_MS)
    const tickId = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000))
    }, 1000)

    return () => {
      clearInterval(pollId)
      clearInterval(tickId)
    }
  }, [warehouseReady, checkHealth, cached])

  return {
    warehouseReady,
    elapsed,
    statusMessage: getStatusMessage(elapsed),
    showSlowWarning: elapsed >= SLOW_SEC,
    retryNow,
  }
}

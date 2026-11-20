import { useCallback, useEffect, useRef, useState } from 'react'
import {
  SERVICES,
  fetchAnomalies,
  fetchAnomalySummary,
  fetchBackendHealth,
  fetchInvestigations,
  fetchInvestigationSummary,
  fetchModelStatus,
  fetchServiceHealth,
  fetchTelemetry,
  fetchTelemetrySummary,
} from '../services/api.js'

const EMPTY_SNAPSHOT = {
  backend: null,
  telemetry: null,
  telemetrySummary: null,
  model: null,
  anomalies: null,
  anomalySummary: null,
  investigations: null,
  investigationSummary: null,
  serviceHealth: {},
  errors: {},
  loading: true,
  lastUpdated: null,
}

export default function useMonitoringData(pollInterval = 5000) {
  const [snapshot, setSnapshot] = useState(EMPTY_SNAPSHOT)
  const [refreshToken, setRefreshToken] = useState(0)
  const inFlight = useRef(false)

  useEffect(() => {
    let mounted = true
    let controller

    async function refresh() {
      if (inFlight.current) return
      inFlight.current = true
      controller = new AbortController()
      const signal = controller.signal
      const requests = [
        ['backend', fetchBackendHealth(signal)],
        ['telemetry', fetchTelemetry(signal)],
        ['telemetrySummary', fetchTelemetrySummary(signal)],
        ['model', fetchModelStatus(signal)],
        ['anomalies', fetchAnomalies(signal)],
        ['anomalySummary', fetchAnomalySummary(signal)],
        ['investigations', fetchInvestigations(signal)],
        ['investigationSummary', fetchInvestigationSummary(signal)],
        ...SERVICES.map((service) => [
          `service:${service.id}`,
          fetchServiceHealth(service, signal),
        ]),
      ]

      try {
        const results = await Promise.allSettled(requests.map(([, request]) => request))
        if (!mounted || signal.aborted) return
        setSnapshot((current) => {
          const next = { ...current, errors: {}, loading: false, lastUpdated: new Date().toISOString() }
          results.forEach((result, index) => {
            const key = requests[index][0]
            if (result.status === 'fulfilled') {
              if (key.startsWith('service:')) {
                next.serviceHealth = { ...next.serviceHealth, [key.slice(8)]: result.value }
              } else {
                next[key] = result.value
              }
            } else if (result.reason?.name !== 'AbortError') {
              next.errors[key] = result.reason?.message || 'Request failed.'
            }
          })
          return next
        })
      } finally {
        inFlight.current = false
      }
    }

    refresh()
    const timer = window.setInterval(refresh, pollInterval)
    return () => {
      mounted = false
      window.clearInterval(timer)
      controller?.abort()
    }
  }, [pollInterval, refreshToken])

  const refresh = useCallback(() => setRefreshToken((value) => value + 1), [])
  return { snapshot, refresh }
}

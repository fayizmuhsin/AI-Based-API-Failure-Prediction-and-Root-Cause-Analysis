const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api').replace(/\/$/, '')

export const SERVICES = [
  { id: 'api-gateway', name: 'API Gateway', short: 'GW', port: 8100 },
  { id: 'user-service', name: 'User Service', short: 'US', port: 8101 },
  { id: 'order-service', name: 'Order Service', short: 'OS', port: 8102 },
  { id: 'database-service', name: 'Database Service', short: 'DB', port: 8103 },
]

export async function fetchJson(url, signal) {
  let response
  const requestController = new AbortController()
  const abortFromOwner = () => requestController.abort()
  signal?.addEventListener('abort', abortFromOwner, { once: true })
  const timeout = setTimeout(() => requestController.abort(), 4000)
  try {
    response = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal: requestController.signal,
    })
  } catch (error) {
    if (error.name === 'AbortError' && signal?.aborted) throw error
    if (error.name === 'AbortError') throw new Error('The local service did not respond within 4 seconds.')
    throw new Error('Unable to connect. Check that the backend or service is running and reachable.')
  } finally {
    clearTimeout(timeout)
    signal?.removeEventListener('abort', abortFromOwner)
  }
  if (!response.ok) throw new Error(`Request failed with HTTP ${response.status}.`)
  return response.json()
}

export const apiUrl = (path) => `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`

export const fetchBackendHealth = (signal) => fetchJson(apiUrl('/health'), signal)
export const fetchTelemetry = (signal) => fetchJson(apiUrl('/telemetry/recent?limit=500'), signal)
export const fetchTelemetrySummary = (signal) => fetchJson(apiUrl('/telemetry/summary'), signal)
export const fetchModelStatus = (signal) => fetchJson(apiUrl('/anomalies/model'), signal)
export const fetchAnomalies = (signal) => fetchJson(apiUrl('/anomalies/recent?limit=100'), signal)
export const fetchAnomalySummary = (signal) => fetchJson(apiUrl('/anomalies/summary'), signal)
export const fetchInvestigations = (signal) => fetchJson(apiUrl('/rca/recent?limit=100'), signal)
export const fetchInvestigationSummary = (signal) => fetchJson(apiUrl('/rca/summary'), signal)
export const fetchAnomalyDetail = (id, signal) => fetchJson(apiUrl(`/anomalies/${encodeURIComponent(id)}`), signal)
export const fetchInvestigationDetail = (id, signal) => fetchJson(apiUrl(`/rca/${encodeURIComponent(id)}`), signal)
export const fetchInvestigationCandidates = (id, signal) =>
  fetchJson(apiUrl(`/rca/${encodeURIComponent(id)}/candidates`), signal)

export function fetchServiceHealth(service, signal) {
  const started = performance.now()
  return fetchJson(apiUrl(`/services/${encodeURIComponent(service.id)}/health`), signal).then((result) => ({
    ...result,
    response_time_ms: performance.now() - started,
  }))
}

export function serviceStatus(snapshot, serviceId) {
  if (snapshot.errors[`service:${serviceId}`]) return 'unavailable'
  const result = snapshot.serviceHealth[serviceId]
  if (!result) return 'loading'
  return result.status === 'ok' ? 'healthy' : 'unhealthy'
}

export function telemetryItems(snapshot) {
  return snapshot.telemetry?.items || []
}

export function metricItems(snapshot, serviceId = '') {
  return telemetryItems(snapshot).filter((item) =>
    item.type === 'metric'
    && item.metric_name === 'http.server.request'
    && (!serviceId || item.service_id === serviceId),
  )
}

export function serviceMetrics(snapshot, serviceId) {
  const rows = metricItems(snapshot, serviceId)
  const failures = rows.filter((item) => item.timed_out || (item.status_code || 0) >= 400)
  const durations = rows.map((item) => Number(item.duration_ms)).filter(Number.isFinite).sort((a, b) => a - b)
  return {
    requestCount: rows.length,
    errorCount: failures.length,
    errorRate: rows.length ? failures.length / rows.length : null,
    averageLatency: durations.length
      ? durations.reduce((total, value) => total + value, 0) / durations.length
      : null,
    p95Latency: percentile(durations, 0.95),
    latestTimestamp: rows.reduce((latest, item) => item.timestamp > latest ? item.timestamp : latest, ''),
  }
}

export function telemetrySeries(snapshot, serviceId = '') {
  const buckets = new Map()
  metricItems(snapshot, serviceId).forEach((item) => {
    const minute = String(item.timestamp || '').slice(0, 16)
    if (!minute) return
    const key = `${item.service_id}:${minute}`
    const bucket = buckets.get(key) || { time: minute, service_id: item.service_id, requests: 0, failures: 0, durations: [] }
    bucket.requests += 1
    if (item.timed_out || (item.status_code || 0) >= 400) bucket.failures += 1
    const duration = Number(item.duration_ms)
    if (Number.isFinite(duration)) bucket.durations.push(duration)
    buckets.set(key, bucket)
  })
  return [...buckets.values()].sort((a, b) => a.time.localeCompare(b.time) || a.service_id.localeCompare(b.service_id)).map((bucket) => ({
    ...bucket,
    errorRate: bucket.requests ? bucket.failures / bucket.requests : 0,
    p95Latency: percentile(bucket.durations.sort((a, b) => a - b), 0.95),
  }))
}

export function percentile(sortedValues, quantile) {
  if (!sortedValues.length) return null
  const position = (sortedValues.length - 1) * quantile
  const lower = Math.floor(position)
  const upper = Math.ceil(position)
  return sortedValues[lower] + (sortedValues[upper] - sortedValues[lower]) * (position - lower)
}

export function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function formatDuration(value) {
  return Number.isFinite(value) ? `${value.toFixed(value >= 100 ? 0 : 1)} ms` : '—'
}

export function formatPercent(value) {
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—'
}

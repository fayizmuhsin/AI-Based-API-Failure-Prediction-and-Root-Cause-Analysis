import StatusBadge from './StatusBadge.jsx'
import { formatDuration, formatPercent } from '../services/format.js'

export default function ServiceCard({ service, status, health, metrics, onClick, selected = false }) {
  const endpoint = `http://127.0.0.1:${service.port}/health`
  return (
    <button className={`service-card ${selected ? 'service-card-selected' : ''}`} onClick={onClick} type="button">
      <div className="service-card-top">
        <span className={`service-glyph service-glyph-${status}`} aria-hidden="true">{service.short}</span>
        <StatusBadge status={status} compact />
      </div>
      <div className="service-name">{service.name}</div>
      <div className="service-endpoint">127.0.0.1:{service.port}</div>
      <div className="service-mini-stats">
        <span><small>Requests</small><strong>{metrics.requestCount || '—'}</strong></span>
        <span><small>Error rate</small><strong className={metrics.errorRate > 0.1 ? 'text-danger' : ''}>{formatPercent(metrics.errorRate)}</strong></span>
        <span><small>P95 latency</small><strong>{formatDuration(metrics.p95Latency)}</strong></span>
      </div>
      <div className="service-card-foot">
        <span>{health?.status === 'ok' ? `Health check · ${formatDuration(health.response_time_ms)}` : status === 'unavailable' ? 'Health endpoint unreachable' : 'Awaiting health check'}</span>
        <span className="service-card-arrow">↗</span>
      </div>
      <span className="sr-only">Open details for {service.name}; endpoint {endpoint}</span>
    </button>
  )
}

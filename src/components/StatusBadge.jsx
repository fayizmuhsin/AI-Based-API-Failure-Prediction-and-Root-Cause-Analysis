const LABELS = {
  healthy: 'Healthy',
  unhealthy: 'Unhealthy',
  unavailable: 'Unavailable',
  loading: 'Checking',
  connected: 'Connected',
  disconnected: 'Disconnected',
  completed: 'Completed',
  inconclusive: 'Inconclusive',
  critical: 'Critical',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
}

export default function StatusBadge({ status = 'loading', compact = false }) {
  const tone = ['healthy', 'connected', 'completed'].includes(status)
    ? 'good'
    : ['critical', 'unhealthy', 'unavailable', 'disconnected'].includes(status)
      ? 'bad'
      : ['high', 'medium', 'inconclusive'].includes(status)
        ? 'warn'
        : 'muted'
  return (
    <span className={`status-badge status-${tone} ${compact ? 'status-compact' : ''}`}>
      <span className="status-dot" />{LABELS[status] || status}
    </span>
  )
}

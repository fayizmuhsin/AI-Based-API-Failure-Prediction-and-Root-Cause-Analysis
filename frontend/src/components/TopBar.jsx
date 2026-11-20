import StatusBadge from './StatusBadge.jsx'
import { formatDate } from '../services/format.js'

export default function TopBar({ pageTitle, backendStatus, lastUpdated, onRefresh, refreshing }) {
  return (
    <header className="topbar">
      <div className="topbar-title">
        <span className="mobile-brand">SIGNAL<span>OPS</span></span>
        <div>
          <p className="breadcrumb">OBSERVABILITY <span>/</span> LOCAL CLUSTER</p>
          <h1>{pageTitle}</h1>
        </div>
      </div>
      <div className="topbar-actions">
        <div className="last-updated"><span>Last updated</span><strong>{formatDate(lastUpdated)}</strong></div>
        <button className="refresh-button" onClick={onRefresh} disabled={refreshing} aria-label="Refresh dashboard data">
          <span className={refreshing ? 'refresh-spin' : ''}>↻</span><span className="refresh-label">Refresh</span>
        </button>
        <StatusBadge status={backendStatus} />
      </div>
    </header>
  )
}

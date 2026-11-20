const ITEMS = [
  { id: 'overview', label: 'Overview', icon: '◫' },
  { id: 'services', label: 'Services', icon: '▦' },
  { id: 'telemetry', label: 'Telemetry', icon: '⌁' },
  { id: 'anomalies', label: 'Anomalies', icon: '⌁' },
  { id: 'rca', label: 'Root Cause Analysis', icon: '◎' },
  { id: 'incidents', label: 'Incidents', icon: '◷' },
]

export default function Sidebar({ page, onNavigate, anomaliesCount }) {
  return (
    <aside className="sidebar">
      <a className="brand" href="#overview" onClick={(event) => { event.preventDefault(); onNavigate('overview') }}>
        <span className="brand-mark"><span /></span>
        <span className="brand-copy">
          <strong>SIGNAL<span>OPS</span></strong>
          <small>API RELIABILITY</small>
        </span>
      </a>
      <div className="sidebar-label">MONITORING</div>
      <nav aria-label="Dashboard navigation" className="side-nav">
        {ITEMS.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => onNavigate(item.id)}
            className={`side-link ${page === item.id ? 'side-link-active' : ''}`}
            aria-current={page === item.id ? 'page' : undefined}
          >
            <span className="side-icon" aria-hidden="true">{item.icon}</span>
            <span>{item.label}</span>
            {item.id === 'anomalies' && anomaliesCount > 0 && <span className="nav-count">{anomaliesCount}</span>}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <span className="local-indicator" />
        <span><strong>Local environment</strong><small>All systems run on this machine</small></span>
      </div>
    </aside>
  )
}

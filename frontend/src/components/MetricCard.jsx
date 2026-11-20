export default function MetricCard({ label, value, detail, tone = 'teal', icon }) {
  return (
    <article className="metric-card">
      <div className="metric-head">
        <span className="metric-label">{label}</span>
        <span className={`metric-icon metric-${tone}`}>{icon}</span>
      </div>
      <div className="metric-value">{value}</div>
      <div className="metric-detail">{detail}</div>
    </article>
  )
}

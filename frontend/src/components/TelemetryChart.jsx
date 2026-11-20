const COLORS = ['#45d6c4', '#64a8ff', '#f5b942', '#f47777']

export default function TelemetryChart({ title, subtitle, rows, valueKey, format = (value) => String(value), serviceIds = [] }) {
  const width = 720
  const height = 210
  const left = 48
  const right = 14
  const top = 14
  const bottom = 30
  const chartWidth = width - left - right
  const chartHeight = height - top - bottom
  const times = [...new Set(rows.map((row) => row.time))].sort()
  const maximum = Math.max(1, ...rows.map((row) => Number(row[valueKey]) || 0))

  return (
    <section className="panel chart-panel">
      <div className="panel-heading">
        <div><h2>{title}</h2><p>{subtitle}</p></div>
        <span className="live-caption"><i /> OBSERVED</span>
      </div>
      {times.length === 0 ? (
        <div className="chart-empty">No request metrics have been collected yet.</div>
      ) : (
        <>
          <div className="chart-area">
            <svg className="chart-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={title}>
              {[0, 1, 2, 3].map((tick) => {
                const y = top + (chartHeight * tick) / 3
                const value = maximum - (maximum * tick) / 3
                return <g key={tick}>
                  <line x1={left} x2={width - right} y1={y} y2={y} className="chart-grid" />
                  <text x={left - 8} y={y + 4} textAnchor="end" className="chart-axis-label">{format(value)}</text>
                </g>
              })}
              {serviceIds.map((serviceId, index) => {
                const coordinates = times.map((time, timeIndex) => {
                  const row = rows.find((item) => item.time === time && item.service_id === serviceId)
                  if (!row) return null
                  const x = left + (times.length === 1 ? chartWidth / 2 : (chartWidth * timeIndex) / (times.length - 1))
                  const y = top + chartHeight - ((Number(row[valueKey]) || 0) / maximum) * chartHeight
                  return { x, y }
                })
                if (!coordinates.some(Boolean)) return null
                let drawing = false
                const path = coordinates.map((point) => {
                  if (!point) { drawing = false; return '' }
                  const command = `${drawing ? 'L' : 'M'} ${point.x} ${point.y}`
                  drawing = true
                  return command
                }).join(' ')
                return <g key={serviceId}>
                  <path d={path} fill="none" stroke={COLORS[index % COLORS.length]} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
                  {coordinates.filter(Boolean).map((point, pointIndex) => <circle key={pointIndex} cx={point.x} cy={point.y} r="2.3" fill={COLORS[index % COLORS.length]} />)}
                </g>
              })}
              <text x={left} y={height - 7} className="chart-axis-label">{times[0]?.replace('T', ' ')}</text>
              {times.length > 1 && <text x={width - right} y={height - 7} textAnchor="end" className="chart-axis-label">{times.at(-1)?.replace('T', ' ')}</text>}
            </svg>
          </div>
          <div className="chart-legend">
            {serviceIds.map((serviceId, index) => <span key={serviceId}><i style={{ background: COLORS[index % COLORS.length] }} />{serviceId}</span>)}
          </div>
        </>
      )}
    </section>
  )
}

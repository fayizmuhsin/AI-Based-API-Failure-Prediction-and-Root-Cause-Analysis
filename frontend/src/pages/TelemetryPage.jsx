import { useMemo, useState } from 'react'
import PageHeading from '../components/PageHeading.jsx'
import StatePanel from '../components/StatePanel.jsx'
import { SERVICES } from '../services/api.js'
import { formatDate } from '../services/format.js'

export default function TelemetryPage({ snapshot }) {
  const [serviceFilter, setServiceFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const items = snapshot.telemetry?.items || []
  const filtered = useMemo(() => items.filter((item) =>
    (serviceFilter === 'all' || item.service_id === serviceFilter)
    && (typeFilter === 'all' || item.type === typeFilter),
  ), [items, serviceFilter, typeFilter])

  return (
    <>
      <PageHeading eyebrow="Signals / persisted observations" title="Telemetry"
        description="Metrics, distributed trace spans, and structured events stored by the monitoring API." />
      <section className="panel telemetry-panel">
        <div className="toolbar-row">
          <div><h2>Recent records</h2><p>{snapshot.telemetry ? `${filtered.length} shown · ${snapshot.telemetry.stored_records} stored` : 'Waiting for telemetry response'}</p></div>
          <div className="filters">
            <label>Service<select value={serviceFilter} onChange={(event) => setServiceFilter(event.target.value)}>
              <option value="all">All services</option>{SERVICES.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}
            </select></label>
            <label>Type<select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
              <option value="all">All types</option><option value="metric">Metrics</option><option value="trace">Traces</option><option value="log">Logs / events</option>
            </select></label>
          </div>
        </div>
        {snapshot.errors.telemetry && !snapshot.telemetry ? <StatePanel kind="error" title="Telemetry could not be loaded" message={snapshot.errors.telemetry} />
          : filtered.length === 0 ? <StatePanel title="No matching telemetry" message="Try a different filter or wait for the services to send observed telemetry." />
            : <div className="table-scroll"><table className="data-table telemetry-table">
              <thead><tr><th>Service / type</th><th>Timestamp</th><th>Trace ID</th><th>Request or event details</th><th>Result</th></tr></thead>
              <tbody>{filtered.map((item) => <tr key={item.event_id || item.id}>
                <td><strong>{item.service_name || item.service_id}</strong><span className={`type-chip type-${item.type}`}>{item.type}</span></td>
                <td className="nowrap">{formatDate(item.timestamp)}</td>
                <td><code className="trace-id">{item.trace_id || '—'}</code></td>
                <td><strong>{item.metric_name || item.event_type || item.name || 'Telemetry record'}</strong>
                  <small className="cell-sub">{item.method || ''} {item.route || ''}{item.dependency ? ` → ${item.dependency}` : ''}{item.error_details ? ` · ${item.error_details}` : ''}</small></td>
                <td><span className={(item.timed_out || (item.status_code || 0) >= 500 || item.status === 'ERROR') ? 'text-danger' : 'text-subtle'}>
                  {item.timed_out ? 'Timeout' : item.status_code || item.status || item.level || '—'}
                </span></td>
              </tr>)}</tbody>
            </table></div>}
      </section>
      <p className="data-note">Showing the backend's most recent persisted telemetry sample, filtered in the browser. The API currently returns up to 500 records.</p>
    </>
  )
}

import PageHeading from '../components/PageHeading.jsx'
import ServiceCard from '../components/ServiceCard.jsx'
import StatePanel from '../components/StatePanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { SERVICES } from '../services/api.js'
import { formatDate, formatDuration, formatPercent, serviceMetrics, serviceStatus, telemetryItems } from '../services/format.js'

export default function ServicesPage({ snapshot, selectedService, onSelectService, onSelectAnomaly }) {
  const service = SERVICES.find((item) => item.id === selectedService) || SERVICES[0]
  const metrics = serviceMetrics(snapshot, service.id)
  const health = snapshot.serviceHealth[service.id]
  const activity = telemetryItems(snapshot).filter((item) => item.service_id === service.id).slice(0, 12)
  const anomalies = snapshot.anomalies?.items?.filter((item) => item.service_id === service.id) || []
  const status = serviceStatus(snapshot, service.id)

  return (
    <>
      <PageHeading eyebrow="Infrastructure / service inventory" title="Services"
        description="Each health state is read from that service's local HTTP health endpoint. Request metrics are based on collected telemetry." />
      <div className="service-grid service-grid-large">
        {SERVICES.map((item) => <ServiceCard key={item.id} service={item} status={serviceStatus(snapshot, item.id)}
          health={snapshot.serviceHealth[item.id]} metrics={serviceMetrics(snapshot, item.id)}
          selected={item.id === service.id} onClick={() => onSelectService(item.id)} />)}
      </div>

      <section className="panel service-detail-panel section-block">
        <div className="panel-heading"><div><p className="eyebrow">Selected service</p><h2>{service.name}</h2>
          <p>{`http://127.0.0.1:${service.port}/health`}</p></div><StatusBadge status={status} /></div>
        <div className="service-detail-stats">
          <DetailMetric label="Port" value={service.port} />
          <DetailMetric label="Health-check response" value={formatDuration(health?.response_time_ms)} />
          <DetailMetric label="Recent requests" value={snapshot.telemetry ? metrics.requestCount : '—'} />
          <DetailMetric label="Observed error rate" value={formatPercent(metrics.errorRate)} />
          <DetailMetric label="Observed P95 latency" value={formatDuration(metrics.p95Latency)} />
          <DetailMetric label="Last request" value={formatDate(metrics.latestTimestamp)} />
          <DetailMetric label="Last health result" value={health?.status || (snapshot.errors[`service:${service.id}`] ? 'Unreachable' : 'Pending')} />
        </div>
        {snapshot.errors[`service:${service.id}`] && <div className="inline-error">{snapshot.errors[`service:${service.id}`]}</div>}
        <div className="service-health-details">
          <div><h3>Health response</h3><p>Actual response from the service endpoint.</p></div>
          {health ? <pre className="json-view">{JSON.stringify(health, null, 2)}</pre>
            : <StatePanel kind={snapshot.errors[`service:${service.id}`] ? 'error' : 'loading'}
              title={snapshot.errors[`service:${service.id}`] ? 'Health check failed' : 'Checking endpoint'}
              message={snapshot.errors[`service:${service.id}`] || 'Waiting for the next live health response.'} />}
        </div>
      </section>

      <div className="two-column section-block">
        <section className="panel">
          <div className="panel-heading"><div><h2>Recent activity</h2><p>Records received from this service</p></div></div>
          {snapshot.errors.telemetry && !snapshot.telemetry ? <StatePanel kind="error" title="Telemetry unavailable" message={snapshot.errors.telemetry} />
            : activity.length === 0 ? <StatePanel title="No recent activity" message="This service has not sent telemetry in the current sample." />
              : <div className="activity-list">{activity.map((item) => <div className="activity-row" key={item.event_id || item.id}>
                <span className={`type-chip type-${item.type}`}>{item.type}</span>
                <div><strong>{item.metric_name || item.event_type || item.name || 'Telemetry record'}</strong>
                  <small>{formatDate(item.timestamp)} {item.route || item.dependency || ''}</small></div>
                <span className="activity-result">{item.status_code || item.status || '—'}</span>
              </div>)}</div>}
        </section>
        <section className="panel">
          <div className="panel-heading"><div><h2>Relevant anomalies</h2><p>Detection records for {service.name}</p></div></div>
          {anomalies.length === 0 ? <StatePanel title="No anomalies for this service" message="No matching persisted anomaly is present in the recent result set." />
            : <div className="compact-list">{anomalies.map((item) => <button className="compact-row" key={item.anomaly_id}
              onClick={() => onSelectAnomaly(item.anomaly_id)}><span className="row-main"><strong>Anomaly #{item.anomaly_id}</strong>
                <small>{formatDate(item.window_start)} · score {item.model_score ?? '—'}</small></span>
              <StatusBadge status={item.severity} compact /></button>)}</div>}
        </section>
      </div>
    </>
  )
}

function DetailMetric({ label, value }) {
  return <div className="detail-metric"><small>{label}</small><strong>{value}</strong></div>
}

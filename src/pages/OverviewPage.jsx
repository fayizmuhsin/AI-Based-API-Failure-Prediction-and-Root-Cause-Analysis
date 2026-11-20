import MetricCard from '../components/MetricCard.jsx'
import ServiceCard from '../components/ServiceCard.jsx'
import DependencyGraph from '../components/DependencyGraph.jsx'
import PageHeading from '../components/PageHeading.jsx'
import StatePanel from '../components/StatePanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import TelemetryChart from '../components/TelemetryChart.jsx'
import { SERVICES } from '../services/api.js'
import { formatDate, formatDuration, formatPercent, metricItems, serviceMetrics, serviceStatus, telemetrySeries } from '../services/format.js'

export default function OverviewPage({ snapshot, onNavigate, onSelectService, onSelectAnomaly }) {
  const observed = SERVICES.some((service) => snapshot.serviceHealth[service.id] || snapshot.errors[`service:${service.id}`])
  const healthy = SERVICES.filter((service) => serviceStatus(snapshot, service.id) === 'healthy').length
  const unhealthy = SERVICES.filter((service) => ['unhealthy', 'unavailable'].includes(serviceStatus(snapshot, service.id))).length
  const anomalies = snapshot.anomalies?.items || []
  const chartRows = telemetrySeries(snapshot)
  const serviceIds = SERVICES.map((service) => service.id)
  const metricRecords = metricItems(snapshot)

  return (
    <>
      <PageHeading eyebrow="Local environment / live monitoring" title="Reliability overview"
        description="Service health and operational signals collected from your local monitoring stack." />

      {snapshot.errors.backend && <div className="connection-banner"><span className="status-dot" />
        <div><strong>Monitoring API is unreachable</strong><p>{snapshot.errors.backend}</p></div>
      </div>}

      <section className="metrics-grid" aria-label="System summary">
        <MetricCard label="Total services" value={SERVICES.length} detail="Configured local services" icon="▦" />
        <MetricCard label="Healthy services" value={observed ? healthy : '—'} detail={observed ? `${healthy} of ${SERVICES.length} responding` : 'Waiting for service checks'} tone="green" icon="✓" />
        <MetricCard label="Unhealthy / unavailable" value={observed ? unhealthy : '—'} detail={observed ? 'Based on live health endpoints' : 'Waiting for service checks'} tone={unhealthy ? 'red' : 'blue'} icon="!" />
        <MetricCard label="Recent anomalies" value={snapshot.anomalies ? anomalies.length : '—'} detail="Persisted Phase 5 detections" tone="amber" icon="⌁" />
        <MetricCard label="Investigations" value={snapshot.investigationSummary?.total_investigations ?? '—'} detail="Persisted RCA reports" tone="blue" icon="◎" />
      </section>

      <section className="section-block">
        <div className="section-title-row"><div><p className="eyebrow">Infrastructure</p><h2>Service health</h2></div>
          <button className="text-link" onClick={() => onNavigate('services')}>View all services <span>→</span></button></div>
        {!observed && snapshot.loading ? <StatePanel kind="loading" title="Checking local services" message="Health status comes from each service's own /health endpoint." /> : (
          <div className="service-grid">
            {SERVICES.map((service) => <ServiceCard key={service.id} service={service}
              status={serviceStatus(snapshot, service.id)} health={snapshot.serviceHealth[service.id]}
              metrics={serviceMetrics(snapshot, service.id)} onClick={() => onSelectService(service.id)} />)}
          </div>
        )}
      </section>

      <div className="overview-grid section-block">
        <DependencyGraph snapshot={snapshot} onSelectService={onSelectService} />
        <section className="panel overview-recent">
          <div className="panel-heading"><div><h2>Recent anomalies</h2><p>Latest persisted detections</p></div>
            <button className="text-link" onClick={() => onNavigate('anomalies')}>All anomalies →</button></div>
          {snapshot.errors.anomalies && !snapshot.anomalies ? <StatePanel kind="error" title="Anomalies unavailable" message={snapshot.errors.anomalies} />
            : anomalies.length === 0 ? <StatePanel title="No anomalies recorded" message="Detections will appear here when the monitoring worker identifies an anomaly." />
              : <div className="compact-list">{anomalies.slice(0, 5).map((item) => {
                const service = SERVICES.find((entry) => entry.id === item.service_id)
                return <button className="compact-row" key={item.anomaly_id} onClick={() => onSelectAnomaly(item.anomaly_id)}>
                  <span className="row-main"><strong>{service?.name || item.service_id}</strong><small>{formatDate(item.window_start)}</small></span>
                  <StatusBadge status={item.severity || 'low'} compact />
                </button>
              })}</div>}
          <div className="recent-foot"><span>{metricRecords.length} request metrics in recent sample</span></div>
        </section>
      </div>

      <section className="charts-grid section-block">
        <TelemetryChart title="Request volume" subtitle="Server requests per observed minute" rows={chartRows}
          valueKey="requests" serviceIds={serviceIds} format={(value) => Math.round(value).toString()} />
        <TelemetryChart title="Error rate" subtitle="Observed failed or timed-out requests" rows={chartRows}
          valueKey="errorRate" serviceIds={serviceIds} format={formatPercent} />
        <TelemetryChart title="Response-time trend" subtitle="P95 server request latency by minute" rows={chartRows}
          valueKey="p95Latency" serviceIds={serviceIds} format={(value) => formatDuration(value)} />
      </section>
      <p className="data-note">Charts use the latest {snapshot.telemetry?.items?.length ?? 0} persisted telemetry records. Missing values are left blank; no sample data is generated.</p>
    </>
  )
}

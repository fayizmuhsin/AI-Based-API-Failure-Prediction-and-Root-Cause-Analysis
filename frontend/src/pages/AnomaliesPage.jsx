import PageHeading from '../components/PageHeading.jsx'
import StatePanel from '../components/StatePanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { SERVICES } from '../services/api.js'
import { formatDate } from '../services/format.js'

export default function AnomaliesPage({ snapshot, selectedAnomalyId, anomalyDetail, detailLoading, detailError, onSelectAnomaly }) {
  const items = snapshot.anomalies?.items || []
  const selected = anomalyDetail?.anomaly_id === selectedAnomalyId ? anomalyDetail : null
  return (
    <>
      <PageHeading eyebrow="Detection / Phase 5" title="Anomalies"
        description="Persisted model and threshold detections. Anomaly scores are relative ranking signals, not failure probabilities." />
      <div className="anomaly-status-row">
        <section className="panel model-status-panel">
          <div><p className="eyebrow">Detection model</p><h2>Isolation Forest</h2>
            <p>{snapshot.model?.reason || (snapshot.model?.ready ? `Trained on ${snapshot.model.training_sample_count} observed windows.` : 'Model readiness has not been reported.')}</p></div>
          <StatusBadge status={snapshot.model?.ready ? 'healthy' : 'loading'} />
        </section>
        <section className="panel anomaly-total"><span>Total stored detections</span><strong>{snapshot.anomalySummary?.total_anomalies ?? '—'}</strong>
          <small>Recent list shows the latest {items.length} records</small></section>
      </div>
      {snapshot.errors.anomalies && !snapshot.anomalies && <StatePanel kind="error" title="Anomaly data unavailable" message={snapshot.errors.anomalies} />}
      <section className="panel section-block">
        <div className="panel-heading"><div><h2>Recent detections</h2><p>Open a record to inspect its stored features and evidence.</p></div></div>
        {!snapshot.anomalies && !snapshot.errors.anomalies ? <StatePanel kind="loading" title="Loading detections" message="Connecting to the anomaly API." />
          : items.length === 0 ? <StatePanel title="No anomalies recorded" message="The monitoring worker has not persisted an anomaly in the latest result set." />
            : <div className="table-scroll"><table className="data-table">
              <thead><tr><th>ID</th><th>Service</th><th>Detection window</th><th>Severity</th><th>Model score</th><th>Detection signals</th></tr></thead>
              <tbody>{items.map((item) => <tr key={item.anomaly_id} className={selectedAnomalyId === item.anomaly_id ? 'row-selected' : ''}>
                <td><button className="table-link" onClick={() => onSelectAnomaly(item.anomaly_id)}>#{item.anomaly_id}</button></td>
                <td>{SERVICES.find((service) => service.id === item.service_id)?.name || item.service_id}</td>
                <td className="nowrap">{formatDate(item.window_start)}</td>
                <td><StatusBadge status={item.severity} compact /></td>
                <td>{item.model_score == null ? '—' : Number(item.model_score).toFixed(4)}</td>
                <td>{item.threshold_matches?.length ? `${item.threshold_matches.length} rule(s)` : item.model_decision || '—'}</td>
              </tr>)}</tbody>
            </table></div>}
      </section>
      {selectedAnomalyId && <section className="panel detail-panel section-block">
        <div className="panel-heading"><div><p className="eyebrow">Detection evidence</p><h2>Anomaly #{selectedAnomalyId}</h2></div>
          <button className="icon-button" onClick={() => onSelectAnomaly(null)} aria-label="Close anomaly details">×</button></div>
        {detailLoading ? <StatePanel kind="loading" title="Loading anomaly" message="Fetching persisted feature values and rule evidence." />
          : detailError ? <StatePanel kind="error" title="Could not load anomaly" message={detailError} />
            : selected ? <AnomalyEvidence item={selected} /> : <StatePanel title="Select an anomaly" message="Choose an anomaly row to see its evidence." />}
      </section>}
    </>
  )
}

function AnomalyEvidence({ item }) {
  const features = Object.entries(item.feature_values || {})
  return <>
    <div className="evidence-summary">
      <div><small>Service</small><strong>{SERVICES.find((service) => service.id === item.service_id)?.name || item.service_id}</strong></div>
      <div><small>Detection window</small><strong>{formatDate(item.window_start)}</strong></div>
      <div><small>Model decision</small><strong>{item.model_decision || '—'}</strong></div>
      <div><small>Model score</small><strong>{item.model_score == null ? '—' : Number(item.model_score).toFixed(5)}</strong></div>
    </div>
    <p className="score-notice">The Isolation Forest score is an anomaly score. It is not a probability that the service has failed.</p>
    <div className="two-column evidence-columns">
      <div><h3>Observed feature values</h3>{features.length ? <div className="feature-grid">{features.map(([key, value]) =>
        <div className="feature-value" key={key}><small>{key.replaceAll('_', ' ')}</small><strong>{value == null ? 'Not available' : typeof value === 'number' ? value.toFixed(3) : String(value)}</strong></div>)}</div>
        : <StatePanel title="No feature values stored" message="This detection has no persisted feature payload." />}</div>
      <div><h3>Matched threshold rules</h3>{item.threshold_matches?.length ? <div className="rule-list">{item.threshold_matches.map((rule, index) =>
        <div className="rule-card" key={`${rule.rule}-${index}`}><strong>{rule.rule?.replaceAll('_', ' ')}</strong>
          <span>Observed {rule.observed ?? '—'} · limit {rule.threshold ?? '—'}</span><p>{rule.explanation || 'No rule explanation was recorded.'}</p></div>)}
        </div> : <StatePanel title="No threshold rules matched" message="The model decision and threshold rules are recorded separately." />}</div>
    </div>
  </>
}

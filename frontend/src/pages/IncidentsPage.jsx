import PageHeading from '../components/PageHeading.jsx'
import StatePanel from '../components/StatePanel.jsx'
import StatusBadge from '../components/StatusBadge.jsx'
import { SERVICES } from '../services/api.js'
import { formatDate } from '../services/format.js'

export default function IncidentsPage({ snapshot, onSelectAnomaly, onSelectInvestigation }) {
  const investigations = snapshot.investigations?.items || []
  const anomalies = snapshot.anomalies?.items || []
  const history = [
    ...investigations.map((item) => ({ ...item, record_type: 'investigation', time: item.created_at })),
    ...anomalies.map((item) => ({ ...item, record_type: 'anomaly', time: item.detected_at || item.window_start })),
  ].sort((a, b) => String(b.time).localeCompare(String(a.time)))

  return (
    <>
      <PageHeading eyebrow="History / detected events" title="Incidents"
        description="A combined timeline of actual anomaly records and RCA investigations. Anomalies and investigations are separate record types." />
      <section className="panel">
        <div className="panel-heading"><div><h2>Incident history</h2><p>{history.length} records in the current API sample</p></div></div>
        {(snapshot.errors.anomalies && !snapshot.anomalies) || (snapshot.errors.investigations && !snapshot.investigations)
          ? <StatePanel kind="error" title="Some incident data is unavailable" message={snapshot.errors.anomalies || snapshot.errors.investigations} />
          : history.length === 0 ? <StatePanel title="No incidents recorded" message="Persisted anomaly and investigation records will appear here." />
            : <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Record</th><th>Time</th><th>Affected services</th><th>Severity / status</th><th>Probable root cause</th><th /></tr></thead>
              <tbody>{history.map((item) => {
                const isInvestigation = item.record_type === 'investigation'
                const causes = item.root_causes || []
                const serviceName = SERVICES.find((service) => service.id === item.service_id)?.name || item.service_id
                return <tr key={`${item.record_type}-${isInvestigation ? item.investigation_id : item.anomaly_id}`}>
                  <td><span className={`record-kind ${isInvestigation ? 'record-rca' : 'record-anomaly'}`}>{isInvestigation ? 'RCA report' : 'Anomaly'}</span>
                    <strong className="incident-id">{isInvestigation ? item.investigation_id.slice(0, 8) : `#${item.anomaly_id}`}</strong></td>
                  <td className="nowrap">{formatDate(item.time)}</td>
                  <td>{isInvestigation ? (item.affected_services?.length
                    ? item.affected_services.map((id) => SERVICES.find((service) => service.id === id)?.name || id).join(', ')
                    : item.trigger_count ? `${item.trigger_count} triggering anomaly/anomalies` : '—') : serviceName}</td>
                  <td>{isInvestigation ? <StatusBadge status={item.status} compact /> : <StatusBadge status={item.severity} compact />}</td>
                  <td>{isInvestigation ? (causes[0]?.service_name || (item.status === 'inconclusive' ? 'No probable cause' : 'Details available')) : 'Not investigated in this row'}</td>
                  <td><button className="table-link" onClick={() => isInvestigation
                    ? onSelectInvestigation(item.investigation_id)
                    : onSelectAnomaly(item.anomaly_id)}>{isInvestigation ? 'Open report' : 'Open detection'} →</button></td>
                </tr>
              })}</tbody>
            </table></div>}
      </section>
    </>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import Sidebar from './components/Sidebar.jsx'
import TopBar from './components/TopBar.jsx'
import useMonitoringData from './hooks/useMonitoringData.js'
import AnomaliesPage from './pages/AnomaliesPage.jsx'
import IncidentsPage from './pages/IncidentsPage.jsx'
import OverviewPage from './pages/OverviewPage.jsx'
import RcaPage from './pages/RcaPage.jsx'
import ServicesPage from './pages/ServicesPage.jsx'
import TelemetryPage from './pages/TelemetryPage.jsx'
import { fetchAnomalyDetail, fetchInvestigationCandidates, fetchInvestigationDetail, SERVICES } from './services/api.js'

const PAGE_TITLES = {
  overview: 'Overview', services: 'Services', telemetry: 'Telemetry', anomalies: 'Anomalies', rca: 'Root Cause Analysis', incidents: 'Incidents',
}

export default function App() {
  const { snapshot, refresh } = useMonitoringData(5000)
  const [page, setPage] = useState('overview')
  const [selectedService, setSelectedService] = useState(SERVICES[0].id)
  const [selectedAnomalyId, setSelectedAnomalyId] = useState(null)
  const [anomalyDetail, setAnomalyDetail] = useState(null)
  const [anomalyLoading, setAnomalyLoading] = useState(false)
  const [anomalyError, setAnomalyError] = useState('')
  const [selectedInvestigationId, setSelectedInvestigationId] = useState(null)
  const [investigationDetail, setInvestigationDetail] = useState(null)
  const [investigationCandidates, setInvestigationCandidates] = useState(null)
  const [investigationLoading, setInvestigationLoading] = useState(false)
  const [investigationError, setInvestigationError] = useState('')
  const anomalyController = useRef(null)
  const investigationController = useRef(null)

  useEffect(() => () => {
    anomalyController.current?.abort()
    investigationController.current?.abort()
  }, [])

  const selectService = useCallback((serviceId) => {
    setSelectedService(serviceId)
    setPage('services')
  }, [])

  const selectAnomaly = useCallback(async (anomalyId) => {
    anomalyController.current?.abort()
    setSelectedAnomalyId(anomalyId)
    setAnomalyDetail(null)
    setAnomalyError('')
    if (anomalyId == null) { setAnomalyLoading(false); return }
    const controller = new AbortController()
    anomalyController.current = controller
    setAnomalyLoading(true)
    try {
      const detail = await fetchAnomalyDetail(anomalyId, controller.signal)
      if (!controller.signal.aborted) setAnomalyDetail(detail)
    } catch (error) {
      if (error.name !== 'AbortError') setAnomalyError(error.message)
    } finally {
      if (!controller.signal.aborted) setAnomalyLoading(false)
    }
  }, [])

  const selectInvestigation = useCallback(async (investigationId) => {
    investigationController.current?.abort()
    setSelectedInvestigationId(investigationId)
    setInvestigationDetail(null)
    setInvestigationCandidates(null)
    setInvestigationError('')
    if (!investigationId) { setInvestigationLoading(false); return }
    const controller = new AbortController()
    investigationController.current = controller
    setInvestigationLoading(true)
    try {
      const [detail, candidates] = await Promise.all([
        fetchInvestigationDetail(investigationId, controller.signal),
        fetchInvestigationCandidates(investigationId, controller.signal),
      ])
      if (!controller.signal.aborted) {
        setInvestigationDetail(detail)
        setInvestigationCandidates(candidates)
      }
    } catch (error) {
      if (error.name !== 'AbortError') setInvestigationError(error.message)
    } finally {
      if (!controller.signal.aborted) setInvestigationLoading(false)
    }
  }, [])

  const openIncidentAnomaly = useCallback((id) => {
    setPage('anomalies')
    selectAnomaly(id)
  }, [selectAnomaly])
  const openIncidentInvestigation = useCallback((id) => {
    setPage('rca')
    selectInvestigation(id)
  }, [selectInvestigation])

  const backendStatus = snapshot.errors.backend ? 'disconnected' : snapshot.backend ? 'connected' : 'loading'
  const renderPage = () => {
    switch (page) {
      case 'services': return <ServicesPage snapshot={snapshot} selectedService={selectedService} onSelectService={setSelectedService} onSelectAnomaly={openIncidentAnomaly} />
      case 'telemetry': return <TelemetryPage snapshot={snapshot} />
      case 'anomalies': return <AnomaliesPage snapshot={snapshot} selectedAnomalyId={selectedAnomalyId} anomalyDetail={anomalyDetail}
        detailLoading={anomalyLoading} detailError={anomalyError} onSelectAnomaly={selectAnomaly} />
      case 'rca': return <RcaPage snapshot={snapshot} selectedId={selectedInvestigationId} detail={investigationDetail}
        candidates={investigationCandidates} loading={investigationLoading} error={investigationError}
        onSelect={selectInvestigation} onClose={() => selectInvestigation(null)} />
      case 'incidents': return <IncidentsPage snapshot={snapshot} onSelectAnomaly={openIncidentAnomaly} onSelectInvestigation={openIncidentInvestigation} />
      default: return <OverviewPage snapshot={snapshot} onNavigate={setPage} onSelectService={selectService} onSelectAnomaly={openIncidentAnomaly} />
    }
  }

  return (
    <div className="app-shell">
      <Sidebar page={page} onNavigate={setPage} anomaliesCount={snapshot.anomalies?.items?.length || 0} />
      <main className="main-shell">
        <TopBar pageTitle={PAGE_TITLES[page]} backendStatus={backendStatus} lastUpdated={snapshot.lastUpdated}
          onRefresh={refresh} refreshing={snapshot.loading} />
        <div className="page-content">{renderPage()}</div>
        <footer className="app-footer"><span>SignalOps · local observability</span><span>React dashboard · monitoring API :8000</span></footer>
      </main>
    </div>
  )
}

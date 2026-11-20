import StatusBadge from './StatusBadge.jsx'
import { SERVICES } from '../services/api.js'

const NODES = {
  'api-gateway': { x: 50, y: 13 },
  'user-service': { x: 25, y: 47 },
  'order-service': { x: 75, y: 47 },
  'database-service': { x: 50, y: 84 },
}

const EDGES = [
  ['api-gateway', 'user-service', 'M 460 84 L 315 165'],
  ['api-gateway', 'order-service', 'M 540 84 L 685 165'],
  ['order-service', 'user-service', 'M 645 205 L 355 205'],
  ['user-service', 'database-service', 'M 315 245 L 460 325'],
  ['order-service', 'database-service', 'M 685 245 L 540 325'],
]

export default function DependencyGraph({ snapshot, onSelectService }) {
  return (
    <section className="panel dependency-panel">
      <div className="panel-heading">
        <div><h2>Service dependency map</h2><p>Arrows point from caller to dependency</p></div>
        <span className="graph-help">Select a service to inspect</span>
      </div>
      <div className="graph-canvas">
        <svg className="graph-edges" viewBox="0 0 1000 410" preserveAspectRatio="none" aria-hidden="true">
          <defs><marker id="dependency-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="#52667c" />
          </marker></defs>
          {EDGES.map(([caller, dependency, path]) => <path key={`${caller}-${dependency}`} d={path}
            className="dependency-edge" markerEnd="url(#dependency-arrow)" />)}
        </svg>
        {SERVICES.map((service) => {
          const position = NODES[service.id]
          const failed = Boolean(snapshot.errors[`service:${service.id}`])
          const response = snapshot.serviceHealth[service.id]
          const status = failed ? 'unavailable' : !response ? 'loading' : response.status === 'ok' ? 'healthy' : 'unhealthy'
          return (
            <button key={service.id} className={`graph-node graph-node-${status}`}
              style={{ left: `${position.x}%`, top: `${position.y}%` }}
              onClick={() => onSelectService(service.id)} type="button">
              <span className={`graph-node-mark service-glyph-${status}`}>{service.short}</span>
              <span className="graph-node-copy"><strong>{service.name}</strong><small>:{service.port}</small></span>
              <StatusBadge status={status} compact />
            </button>
          )
        })}
      </div>
      <div className="graph-legend"><span><i className="legend-good" />Healthy</span><span><i className="legend-warn" />Checking</span><span><i className="legend-bad" />Unavailable</span></div>
      <p className="graph-note">A dependency issue can affect its upstream callers. The map reflects the configured service topology.</p>
    </section>
  )
}

function StatusMark({ tone }) {
  const colors = {
    loading: 'bg-amber-300 animate-pulse',
    success: 'bg-teal',
    error: 'bg-rose-400',
  }
  return <span aria-hidden="true" className={`h-2.5 w-2.5 rounded-full ${colors[tone]}`} />
}

export default function HealthPanel({ state, onRetry }) {
  const isSuccess = state.status === 'success'
  const isError = state.status === 'error'
  const label = isSuccess ? 'Operational' : isError ? 'Connection issue' : 'Checking connection'

  return (
    <section aria-labelledby="health-heading" className="panel-card p-6 sm:p-8">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div>
          <p className="eyebrow">System health</p>
          <h2 id="health-heading" className="mt-2 text-2xl font-semibold tracking-tight text-white">
            Backend connection
          </h2>
          <p className="mt-2 max-w-xl text-sm leading-6 text-mist">
            Live status from the local FastAPI health endpoint. This foundation does not yet collect service telemetry.
          </p>
        </div>
        <div className={`status-pill ${isSuccess ? 'status-good' : isError ? 'status-bad' : 'status-wait'}`} role="status" aria-live="polite">
          <StatusMark tone={state.status} />
          {label}
        </div>
      </div>

      <div className="mt-8 rounded-xl border border-line bg-ink/70 p-5">
        {state.status === 'loading' && (
          <div className="flex items-center gap-3 text-sm text-slate-300">
            <span className="loading-ring" /> Connecting to the backend…
          </div>
        )}
        {isSuccess && (
          <div className="grid gap-5 sm:grid-cols-3">
            <HealthValue label="Application" value={state.data.application} />
            <HealthValue label="API version" value={state.data.version} />
            <HealthValue label="Environment" value={state.data.environment} />
          </div>
        )}
        {isError && (
          <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
            <div>
              <p className="font-medium text-rose-200">Backend unavailable</p>
              <p className="mt-1 text-sm text-slate-400">{state.error}</p>
            </div>
            <button className="button-secondary" onClick={onRetry}>Try again</button>
          </div>
        )}
      </div>
      {isSuccess && state.data.timestamp && (
        <p className="mt-4 text-xs text-slate-500">
          Last checked <time dateTime={state.data.timestamp}>{new Date(state.data.timestamp).toLocaleString()}</time>
        </p>
      )}
    </section>
  )
}

function HealthValue({ label, value }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-wider text-slate-500">{label}</p>
      <p className="mt-2 truncate text-sm font-medium text-slate-100">{value}</p>
    </div>
  )
}

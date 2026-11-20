export default function StatePanel({ kind = 'empty', title, message, action, onAction }) {
  return (
    <div className={`state-panel state-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      {kind === 'loading' && <span className="loading-ring" />}
      {kind === 'error' && <span className="state-symbol">!</span>}
      {kind === 'empty' && <span className="state-symbol">⌁</span>}
      <strong>{title}</strong>
      <p>{message}</p>
      {action && <button className="button-secondary" onClick={onAction}>{action}</button>}
    </div>
  )
}

import { CheckCircle2, CircleAlert, RefreshCw, X } from 'lucide-react';

export function AppFeedback({ error, actionError, notice, snapshot, pending, refresh, dismissActionError, dismissNotice }) {
  return <div className="app-feedback" aria-live="polite">
    {error && <div role="alert" className="toast error-toast">
      <CircleAlert size={17} />
      <div><strong>Refresh needs attention</strong><p>{error.message}</p>{snapshot && <small>Showing last known data. Email actions are paused until refresh succeeds.</small>}</div>
      <button className="toast-action" onClick={refresh} disabled={Boolean(pending.account)}><RefreshCw size={14} />Retry refresh</button>
    </div>}
    {!error && actionError && <div role="alert" className="toast error-toast">
      <CircleAlert size={17} />
      <div><strong>Action could not finish</strong><p>{actionError.message}</p></div>
      <button type="button" className="toast-dismiss" aria-label="Dismiss error" onClick={dismissActionError}><X size={15} /></button>
    </div>}
    {!error && !actionError && notice && <div role="status" className="toast success-toast">
      <CheckCircle2 size={17} />
      <p>{notice}</p>
      <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={dismissNotice}><X size={15} /></button>
    </div>}
  </div>;
}

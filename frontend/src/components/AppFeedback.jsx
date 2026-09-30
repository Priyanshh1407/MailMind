import { CheckCircle2, CircleAlert, RefreshCw, X } from 'lucide-react';
import { AnimatePresence, m } from 'motion/react';
import { EASE_OUT } from '../motion';

const TOAST_MOTION = {
  initial: { opacity: 0, x: 24 },
  animate: { opacity: 1, x: 0, transition: { duration: 0.32, ease: EASE_OUT } },
  exit: { opacity: 0, x: 24, transition: { duration: 0.18 } },
};

export function AppFeedback({ error, actionError, notice, snapshot, pending, refresh, dismissActionError, dismissNotice }) {
  return <div className="app-feedback" aria-live="polite"><AnimatePresence initial={false}>
    {error && <m.div key="refresh-error" {...TOAST_MOTION} role="alert" className="toast error-toast">
      <CircleAlert size={17} />
      <div><strong>Refresh needs attention</strong><p>{error.message}</p>{snapshot && <small>Showing last known data. Email actions are paused until refresh succeeds.</small>}</div>
      <button className="toast-action" onClick={refresh} disabled={Boolean(pending.account)}><RefreshCw size={14} />Retry refresh</button>
    </m.div>}
    {!error && actionError && <m.div key="action-error" {...TOAST_MOTION} role="alert" className="toast error-toast">
      <CircleAlert size={17} />
      <div><strong>Action could not finish</strong><p>{actionError.message}</p></div>
      <button type="button" className="toast-dismiss" aria-label="Dismiss error" onClick={dismissActionError}><X size={15} /></button>
    </m.div>}
    {!error && !actionError && notice && <m.div key="notice" {...TOAST_MOTION} role="status" className="toast success-toast">
      <CheckCircle2 size={17} />
      <p>{notice}</p>
      <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={dismissNotice}><X size={15} /></button>
    </m.div>}
  </AnimatePresence></div>;
}

import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, m } from 'motion/react';
import { CheckCircle2, CloudOff, OctagonAlert, RefreshCw, RotateCw, Wifi, WifiOff } from 'lucide-react';
import { connectivityProblem, supervisorAlert } from '../dashboard';
import { useSupervisorStatus } from '../hooks/useSupervisorStatus';
import { EASE_OUT } from '../motion';

const MESSAGES = {
  offline: {
    icon: WifiOff, tone: 'warning', title: "You're offline",
    text: "Your saved mail is still here. No need to reconnect Google: MailMind stays signed in and syncs new mail as soon as you're back.",
  },
  gmail: {
    icon: CloudOff, tone: 'warning', title: "Can't reach Gmail",
    text: "You're still connected and nothing is lost. MailMind keeps retrying, waiting a little longer each time; check your internet if this persists.",
  },
  back: {
    icon: Wifi, tone: 'success', title: "You're back online",
    text: 'Checking for new mail…',
  },
  restarting: {
    icon: RotateCw, tone: 'warning', title: 'Restarting a MailMind service',
    text: 'It stopped unexpectedly and is being restarted automatically. Your saved mail is safe.',
  },
  crash_loop: {
    icon: OctagonAlert, tone: 'danger', title: 'MailMind may need to stop',
    text: 'A service keeps failing right after it starts. MailMind is still retrying.',
  },
  recovered: {
    icon: CheckCircle2, tone: 'success', title: 'Everything is running again',
    text: 'MailMind recovered on its own; no action needed.',
  },
};
// Shown first when several things are wrong at once.
const PRIORITY = ['crash_loop', 'offline', 'restarting', 'gmail'];

// Gmail-style system alerts: shown while the device or Gmail is unreachable or
// a MailMind service is restarting, and briefly once things recover. Before
// MailMind ever shuts itself down, this says so, with the countdown.
export function ConnectivityBanner({ snapshot, refresh }) {
  const supervisor = supervisorAlert(useSupervisorStatus());
  const [browserOnline, setBrowserOnline] = useState(() => typeof navigator === 'undefined' || navigator.onLine !== false);
  const [backOnline, setBackOnline] = useState(false);
  const previous = useRef(null);

  useEffect(() => {
    const goOnline = () => { setBrowserOnline(true); refresh?.(); };
    const goOffline = () => setBrowserOnline(false);
    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
    };
  }, [refresh]);

  const connectivity = snapshot?.status?.connectivity;
  const network = connectivityProblem({
    browserOnline,
    connected: Boolean(snapshot?.session?.connected),
    workerErrorCode: snapshot?.status?.worker?.last_error_code,
    gmail: connectivity?.gmail,
  });
  const problem = PRIORITY.find(kind => kind === network || kind === supervisor?.kind) || null;

  // Count down to the worker's next Gmail check (5 s, 10 s, 20 s… up to 60 s).
  const [secondsLeft, setSecondsLeft] = useState(null);
  const retryIn = connectivity?.retry_in_seconds ?? null;
  useEffect(() => {
    setSecondsLeft(retryIn);
    if (retryIn == null) return undefined;
    const timer = window.setInterval(() => setSecondsLeft(value => (value == null || value <= 0 ? 0 : value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [retryIn, snapshot]);

  useEffect(() => {
    let timer = 0;
    if (problem) setBackOnline(false);
    else if (previous.current) {
      // Network problems end with "back online"; service problems with "recovered".
      setBackOnline(['offline', 'gmail'].includes(previous.current) ? 'back' : 'recovered');
      timer = window.setTimeout(() => setBackOnline(false), 4000);
    }
    previous.current = problem;
    return () => window.clearTimeout(timer);
  }, [problem]);

  const key = problem || backOnline || null;
  const message = key && MESSAGES[key];
  const Icon = message?.icon;
  return <div className='connectivity-region' aria-live='polite'>
    {/* One alert at a time: the old one leaves before the next appears. */}
    <AnimatePresence mode='wait' initial={false}>
      {message && <m.div key={key} role='status' className={'connectivity-banner ' + message.tone}
        initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0, transition: { duration: 0.28, ease: EASE_OUT } }}
        exit={{ opacity: 0, y: 16, transition: { duration: 0.18 } }}>
        <Icon size={18} aria-hidden='true' />
        <div><strong>{message.title}</strong><p>{key === 'crash_loop' && supervisor.message ? supervisor.message : message.text}</p>
          {key === 'gmail' && connectivity?.gmail === 'unreachable' && <p className='connectivity-countdown'>
            {secondsLeft ? 'Retrying in ' + secondsLeft + ' s' : 'Checking now…'}
            {connectivity.failed_checks > 1 ? ' · ' + connectivity.failed_checks + ' checks failed' : ''}
          </p>}
          {key === 'restarting' && <p className='connectivity-countdown'>
            {'Restarting ' + supervisor.label + (supervisor.retryIn ? ' in ' + supervisor.retryIn + ' s' : ' now…')}
            {supervisor.restarts > 1 ? ' · restart ' + supervisor.restarts : ''}
          </p>}
          {key === 'crash_loop' && <p className='connectivity-countdown'>
            {supervisor.shutdownIn != null ? 'Shutting down in ' + supervisor.shutdownIn + ' s unless it recovers' : 'Still retrying…'}
          </p>}
        </div>
        {['gmail', 'restarting', 'crash_loop'].includes(key) && <RefreshCw size={14} className='spin-icon connectivity-retry' aria-hidden='true' />}
      </m.div>}
    </AnimatePresence>
  </div>;
}

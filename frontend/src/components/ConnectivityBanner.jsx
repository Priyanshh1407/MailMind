import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, m } from 'motion/react';
import { CheckCircle2, CloudOff, Hourglass, OctagonAlert, RefreshCw, RotateCw, Sparkles, Wifi, WifiOff } from 'lucide-react';
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
  ai_quota: {
    icon: Hourglass, tone: 'warning', title: 'AI free-tier limit reached',
    text: 'Gemini and Groq are refusing more requests for now. Your mail is safe: MailMind sorts the waiting emails automatically once the limits reset. No need to restart or reconnect.',
  },
  ai_busy: {
    icon: Hourglass, tone: 'warning', title: 'AI providers are overloaded',
    text: 'Gemini and Groq are not answering right now. Your mail is safe: MailMind keeps retrying and sorts the waiting emails as soon as they respond.',
  },
  ai_back: {
    icon: Sparkles, tone: 'success', title: 'AI is sorting mail again',
    text: 'The waiting emails are being sorted now.',
  },
};
// Shown first when several things are wrong at once; the AI limits come last,
// since being offline or a crashed service explains them.
const PRIORITY = ['crash_loop', 'offline', 'restarting', 'gmail', 'ai_quota', 'ai_busy'];
const AI_KINDS = ['ai_quota', 'ai_busy'];

function formatWait(seconds) {
  if (seconds < 60) return seconds + ' s';
  const minutes = Math.ceil(seconds / 60);
  return minutes < 60 ? minutes + ' min' : Math.floor(minutes / 60) + ' h ' + (minutes % 60) + ' min';
}

// Gmail-style system alerts: shown while the device or Gmail is unreachable or
// a MailMind service is restarting, and briefly once things recover. Before
// MailMind ever shuts itself down, this says so, with the countdown.
export function ConnectivityBanner({ snapshot, refresh, api, readOnly = false }) {
  const supervisor = supervisorAlert(useSupervisorStatus());
  const [browserOnline, setBrowserOnline] = useState(() => typeof navigator === 'undefined' || navigator.onLine !== false);
  const [backOnline, setBackOnline] = useState(false);
  const previous = useRef(null);
  const [retry, setRetry] = useState({ busy: false, note: '' });

  const retryAll = async () => {
    setRetry({ busy: true, note: '' });
    try {
      const result = await api.request('/tasks/retry-all', { method: 'POST' });
      setRetry({ busy: false, note: result.message });
      refresh?.();
    } catch (failure) {
      setRetry({ busy: false, note: failure.message });
    }
  };

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
  const ai = snapshot?.status?.ai;
  const aiProblem = ai?.state === 'limited' ? (ai.reason === 'busy' ? 'ai_busy' : 'ai_quota') : null;
  const problem = PRIORITY.find(kind => kind === network || kind === supervisor?.kind || kind === aiProblem) || null;

  // Count down to the worker's next Gmail check (5 s, 10 s, 20 s… up to 60 s),
  // or to the next try of the emails waiting for the AI.
  const [secondsLeft, setSecondsLeft] = useState(null);
  const retryIn = (AI_KINDS.includes(problem) ? ai?.retry_in_seconds : connectivity?.retry_in_seconds) ?? null;
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
      setBackOnline(['offline', 'gmail'].includes(previous.current) ? 'back'
        : AI_KINDS.includes(previous.current) ? 'ai_back' : 'recovered');
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
          {AI_KINDS.includes(key) && <p className='connectivity-countdown'>
            {ai?.waiting ? ai.waiting + ' email' + (ai.waiting === 1 ? '' : 's') + ' waiting · ' : ''}
            {secondsLeft ? 'Next try in ' + formatWait(secondsLeft) : 'Trying again now…'}
          </p>}
          {AI_KINDS.includes(key) && api && <p className='connectivity-actions'>
            <button className='button ghost compact' disabled={readOnly || retry.busy} onClick={retryAll}>
              {retry.busy ? 'Queuing…' : 'Retry all now'}
            </button>
            {retry.note && <span>{retry.note}</span>}
          </p>}
          {key === 'crash_loop' && <p className='connectivity-countdown'>
            {supervisor.shutdownIn != null ? 'Shutting down in ' + supervisor.shutdownIn + ' s unless it recovers' : 'Still retrying…'}
          </p>}
        </div>
        {['gmail', 'restarting', 'crash_loop', ...AI_KINDS].includes(key) && <RefreshCw size={14} className='spin-icon connectivity-retry' aria-hidden='true' />}
      </m.div>}
    </AnimatePresence>
  </div>;
}

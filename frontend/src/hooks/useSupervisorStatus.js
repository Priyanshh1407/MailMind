import { useEffect, useState } from 'react';

// Reads the supervisor's status file through the dashboard server, so a
// restarting or failing service can be explained even while the API is down.
// Missing (development server) or unreadable means "nothing to report".
export function useSupervisorStatus({ url = '/mailmind-supervisor.json', interval = 2000 } = {}) {
  const [status, setStatus] = useState(null);
  useEffect(() => {
    let active = true, timer = 0;
    const load = async () => {
      try {
        const response = await fetch(url, { cache: 'no-store' });
        const data = response.ok ? await response.json() : null;
        if (active) setStatus(data);
      } catch {
        if (active) setStatus(null);
      }
      if (active) timer = window.setTimeout(load, interval);
    };
    load();
    return () => { active = false; window.clearTimeout(timer); };
  }, [url, interval]);
  return status;
}

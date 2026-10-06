// Schedule after completion. Stop invalidates every late response.
// After failures, wait longer each time (5 s, 10 s, 20 s, 40 s, then 60 s) so a
// stopped local server isn't hammered; the first success resets the pace.
export const backoffDelay = (failures, interval = 5000, cap = 60000) => Math.min(cap, interval * 2 ** Math.max(0, failures - 1));
export function createPoller(load, { interval = 5000, onData, onError, schedule = setTimeout, cancel = clearTimeout } = {}) {
  let active = false, revision = 0, running = null, timer = null, controller = null, refreshAgain = false, failures = 0;
  function tick() {
    if (!active) return Promise.resolve();
    if (running) { refreshAgain = true; return running; }
    cancel(timer);
    const version = revision;
    const request = new AbortController(); controller = request;
    const current = () => active && version === revision && !request.signal.aborted;
    running = Promise.resolve().then(() => load(request.signal)).then(data => {
      if (current()) { failures = 0; onData?.(data); }
    }).catch(error => {
      if (current() && error.name !== 'AbortError') { failures += 1; onError?.(error); }
    }).finally(() => {
      running = null;
      if (!active) return;
      if (refreshAgain || version !== revision) { refreshAgain = false; tick(); }
      else timer = schedule(tick, failures ? backoffDelay(failures, interval) : interval);
    });
    return running;
  }
  return {
    start() { active = true; return tick(); }, refresh: tick,
    stop() { active = false; revision += 1; refreshAgain = false; cancel(timer); controller?.abort(); },
  };
}

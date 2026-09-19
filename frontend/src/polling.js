// Schedule after completion. Stop invalidates every late response.
export function createPoller(load, { interval = 5000, onData, onError, schedule = setTimeout, cancel = clearTimeout } = {}) {
  let active = false, revision = 0, running = null, timer = null, controller = null, refreshAgain = false;
  function tick() {
    if (!active) return Promise.resolve();
    if (running) { refreshAgain = true; return running; }
    cancel(timer);
    const version = revision;
    const request = new AbortController(); controller = request;
    const current = () => active && version === revision && !request.signal.aborted;
    running = Promise.resolve().then(() => load(request.signal)).then(data => {
      if (current()) onData?.(data);
    }).catch(error => {
      if (current() && error.name !== 'AbortError') onError?.(error);
    }).finally(() => {
      running = null;
      if (!active) return;
      if (refreshAgain || version !== revision) { refreshAgain = false; tick(); }
      else timer = schedule(tick, interval);
    });
    return running;
  }
  return {
    start() { active = true; return tick(); }, refresh: tick,
    stop() { active = false; revision += 1; refreshAgain = false; cancel(timer); controller?.abort(); },
  };
}

import { useEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { formatCount } from '../../dashboard';
import { useBooting } from '../../boot';

// Same curve as EASE_OUT in motion.js, as a plain function.
const easeOutExpo = t => (t >= 1 ? 1 : 1 - 2 ** (-10 * t));
// During power-on the count revs up slowly, races, then settles: a spool-up.
const spoolUp = t => (t < 0.5 ? 8 * t ** 4 : 1 - (-2 * t + 2) ** 4 / 2);

// Counts from the previously shown value to the new one with a plain
// requestAnimationFrame loop (no animation engine in the first-load bundle).
// While the dashboard boots, it waits for the blocks to land, then spools up.
// Missing values show '-', and reduced motion shows the final number at once.
export function AnimatedCount({ value, duration = 600 }) {
  const reduce = useReducedMotion();
  const booting = useBooting();
  const target = value == null ? null : Number(value);
  const shown = useRef(0);
  const [display, setDisplay] = useState(reduce || target == null ? target : 0);

  useEffect(() => {
    if (target == null || !Number.isFinite(target) || reduce || shown.current === target) {
      if (target != null && Number.isFinite(target)) shown.current = target;
      setDisplay(target);
      return undefined;
    }
    const from = shown.current;
    const length = booting ? duration * 2.2 : duration;
    const ease = booting ? spoolUp : easeOutExpo;
    let frame = 0;
    const run = () => {
      const start = performance.now();
      frame = window.requestAnimationFrame(function step(now) {
        const progress = Math.min(1, (now - start) / length);
        shown.current = from + (target - from) * ease(progress);
        setDisplay(progress < 1 ? Math.round(shown.current) : target);
        if (progress < 1) frame = window.requestAnimationFrame(step);
      });
    };
    const timer = booting ? window.setTimeout(run, 900) : 0;
    if (!booting) run();
    return () => {
      window.clearTimeout(timer);
      window.cancelAnimationFrame(frame);
    };
  }, [target, reduce, duration, booting]);

  return formatCount(display);
}

import { useEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'motion/react';
import { formatCount } from '../../dashboard';

// Same curve as EASE_OUT in motion.js, as a plain function.
const easeOutExpo = t => (t >= 1 ? 1 : 1 - 2 ** (-10 * t));

// Counts from the previously shown value to the new one with a plain
// requestAnimationFrame loop (no animation engine in the first-load bundle).
// Missing values show '-', and reduced motion shows the final number at once.
export function AnimatedCount({ value, duration = 600 }) {
  const reduce = useReducedMotion();
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
    const start = performance.now();
    let frame = window.requestAnimationFrame(function step(now) {
      const progress = Math.min(1, (now - start) / duration);
      shown.current = from + (target - from) * easeOutExpo(progress);
      setDisplay(progress < 1 ? Math.round(shown.current) : target);
      if (progress < 1) frame = window.requestAnimationFrame(step);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [target, reduce, duration]);

  return formatCount(display);
}

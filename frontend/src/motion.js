// Shared motion vocabulary. Everything here animates opacity and transform
// only, and <MotionConfig reducedMotion='user'> in main.jsx turns transform
// and layout movement off for visitors who ask for reduced motion.

export const EASE_OUT = [0.16, 1, 0.3, 1];

export const spring = { type: 'spring', stiffness: 420, damping: 36, mass: 0.8 };

export const fadeUp = {
  initial: { opacity: 0, y: 10 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -6, transition: { duration: 0.15 } },
};

// Stagger list entrances, capped so a long page still settles quickly.
export function staggerDelay(index, step = 0.035, max = 0.3) {
  return Math.min(index * step, max);
}

export function enterTransition(index = 0) {
  return { duration: 0.32, ease: EASE_OUT, delay: staggerDelay(index) };
}

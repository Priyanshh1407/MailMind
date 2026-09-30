import { createContext, useContext, useEffect, useState } from 'react';

// Power-on sequence played once each time a connected dashboard comes up.
// The roll-up itself is CSS (.booting in phase6.css); this only says when.
const BOOT_MS = 2600;

export const BootContext = createContext(false);

export const useBooting = () => useContext(BootContext);

// Derived during render so the first connected paint is already booting.
export function useBootSequence(key, enabled) {
  const [bootedKey, setBootedKey] = useState(null);
  const booting = Boolean(enabled && key != null && key !== bootedKey);
  useEffect(() => {
    if (!booting) return undefined;
    const timer = window.setTimeout(() => setBootedKey(key), BOOT_MS);
    return () => window.clearTimeout(timer);
  }, [booting, key]);
  return booting;
}

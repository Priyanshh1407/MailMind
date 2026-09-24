import { BellRing, Laptop } from 'lucide-react';

export function AppFooter({ autoMarkRead }) {
  return <footer className="dashboard-footer">
    <span><Laptop size={13} />Local-first · Your mail stays on this device</span>
    <span><BellRing size={13} />Automatic mark-as-read: {autoMarkRead ? 'On' : 'Off'} · Saved mail refreshed automatically</span>
  </footer>;
}

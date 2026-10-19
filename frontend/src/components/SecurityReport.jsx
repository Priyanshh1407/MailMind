import { ShieldCheck } from 'lucide-react';
import { Surface, SectionTitle } from './ui/Surface';
import report from '../redteam-report.json';

// The committed red-team result (scripts/redteam.py): what a fully hijacked
// AI model could still get past MailMind's own checks.
export function SecurityReport() {
  const total = report.families.reduce((sum, row) => sum + row.cases, 0);
  const without = report.families.reduce((sum, row) => sum + row.harm_without, 0);
  const withLayers = report.families.reduce((sum, row) => sum + row.harm_with, 0);
  return <Surface className='security-report' aria-label='Security report'>
    <SectionTitle icon={<ShieldCheck size={18} />} eyebrow='Red-team, assuming the AI is hijacked' title='What still gets through' />
    <p className='usage-note'>{withLayers} of {total} attacks still reached you with every defense on, against {without} with them off. Each attack assumes an injected instruction fully controls the AI model.</p>
    <div className='table-scroll'>
      <table>
        <thead><tr><th>Attack</th><th>Defenses off</th><th>Defenses on</th></tr></thead>
        <tbody>{report.families.map(row => <tr key={row.family}>
          <th scope='row'>{row.title}</th>
          <td>{row.harm_without}/{row.cases}</td>
          <td className={row.harm_with ? 'residual' : ''}>{row.harm_with}/{row.cases}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {report.families.some(row => row.residual.length) && <p className='usage-note'>Still possible: {report.families.flatMap(row => row.residual).join('; ')}. The answer then cites the attacker&apos;s email, which the scam check usually marks.</p>}
  </Surface>;
}

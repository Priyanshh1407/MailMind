import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { formatMoney, monthLabel } from '../money';

export function MoneyChart({ trend, currency }) {
  const data = trend.map(row => ({ ...row, label: monthLabel(row.month, 'short'), spent: row.spent_minor / 100 }));
  return <div className='token-chart-block'>
    <div className='token-chart' role='img' aria-label={'Monthly spending over the last 12 months in ' + currency}>
      <ResponsiveContainer width='100%' height={240}>
        <BarChart data={data} accessibilityLayer margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray='3 3' stroke='#354156' vertical={false} />
          <XAxis dataKey='label' stroke='#919bad' tickLine={false} axisLine={false} />
          <YAxis stroke='#919bad' tickLine={false} axisLine={false} width={80} tickFormatter={value => formatMoney(Math.round(value * 100), currency)} />
          <Tooltip cursor={{ fill: 'rgba(32, 230, 161, 0.08)' }} contentStyle={{ background: '#111722', border: '1px solid #354156', borderRadius: 10 }} formatter={value => [formatMoney(Math.round(value * 100), currency), 'Spent']} />
          <Bar dataKey='spent' name='Spent' fill='#20e6a1' radius={[6, 6, 0, 0]} maxBarSize={40} />
        </BarChart>
      </ResponsiveContainer>
    </div>
    <details className='chart-table'>
      <summary>View monthly values</summary>
      <div className='table-scroll'>
        <table>
          <thead><tr><th>Month</th><th>Spent</th><th>Refunds</th></tr></thead>
          <tbody>{trend.map(row => <tr key={row.month}>
            <th scope='row'>{monthLabel(row.month)}</th>
            <td>{formatMoney(row.spent_minor, currency)}</td>
            <td>{formatMoney(row.refunds_minor, currency)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </details>
  </div>;
}

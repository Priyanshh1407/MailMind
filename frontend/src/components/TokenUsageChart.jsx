import {
  Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
  XAxis, YAxis,
} from 'recharts';
import { formatCount } from '../dashboard';

export function TokenUsageChart({ daily }) {
  const data = daily.map(row => ({
    ...row,
    label: new Date(row.start_at).toLocaleDateString(
      undefined, { month: 'short', day: 'numeric' },
    ),
  }));
  return <div className='token-chart-block'>
    <div className='token-chart' role='img' aria-label='Daily input and output token trend'>
      <ResponsiveContainer width='100%' height={280}>
        {/* Bars read correctly for one day or many; an area chart of one day is two dots. */}
        <BarChart data={data} accessibilityLayer barGap={4} margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray='3 3' stroke='#354156' vertical={false} />
          <XAxis dataKey='label' stroke='#919bad' tickLine={false} axisLine={false} />
          {/* Thousands-comma'd ticks with room for 7 digits; a narrow axis clipped '80000' to '30000'. */}
          <YAxis stroke='#919bad' tickLine={false} axisLine={false} width={72} tickFormatter={value => formatCount(value)} />
          <Tooltip cursor={{ fill: 'rgba(139, 92, 246, 0.08)' }} contentStyle={{ background: '#111722', border: '1px solid #354156', borderRadius: 10 }} formatter={value => formatCount(value)} />
          <Legend iconType='circle' wrapperStyle={{ paddingTop: 8 }} formatter={value => <span className='chart-legend-text'>{value}</span>} />
          <Bar dataKey='input_tokens' name='Input tokens' fill='#8b5cf6' radius={[6, 6, 0, 0]} maxBarSize={44} />
          <Bar dataKey='output_tokens' name='Output tokens' fill='#2a93d8' radius={[6, 6, 0, 0]} maxBarSize={44} />
        </BarChart>
      </ResponsiveContainer>
    </div>
    <details className='chart-table'>
      <summary>View daily token values</summary>
      <div className='table-scroll'>
        <table>
          <thead><tr><th>Date</th><th>Input</th><th>Output</th><th>Total</th><th>Cloud billed</th><th>Local processed</th></tr></thead>
          <tbody>{daily.map(row => <tr key={row.date}>
            <th scope='row'>{row.date}</th>
            <td>{formatCount(row.input_tokens)}</td>
            <td>{formatCount(row.output_tokens)}</td>
            <td>{formatCount(row.total_tokens)}</td>
            <td>{formatCount(row.provider_billed_tokens)}</td>
            <td>{formatCount(row.local_processed_tokens)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </details>
  </div>;
}

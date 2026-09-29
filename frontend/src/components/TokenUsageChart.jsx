import {
  Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip,
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
        <AreaChart data={data} accessibilityLayer margin={{ top: 12, right: 8, left: 0, bottom: 4 }}>
          <defs>
            <linearGradient id='inputTokenFill' x1='0' y1='0' x2='0' y2='1'>
              <stop offset='5%' stopColor='#8b5cf6' stopOpacity={0.42} />
              <stop offset='95%' stopColor='#8b5cf6' stopOpacity={0.04} />
            </linearGradient>
            <linearGradient id='outputTokenFill' x1='0' y1='0' x2='0' y2='1'>
              <stop offset='5%' stopColor='#47bfff' stopOpacity={0.4} />
              <stop offset='95%' stopColor='#47bfff' stopOpacity={0.04} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray='3 3' stroke='#354156' vertical={false} />
          <XAxis dataKey='label' stroke='#919bad' tickLine={false} axisLine={false} />
          <YAxis stroke='#919bad' tickLine={false} axisLine={false} width={48} />
          <Tooltip contentStyle={{ background: '#111722', border: '1px solid #354156', borderRadius: 10 }} formatter={value => formatCount(value)} />
          <Legend />
          <Area type='monotone' dataKey='input_tokens' name='Input tokens' stroke='#8b5cf6' fill='url(#inputTokenFill)' strokeWidth={2} />
          <Area type='monotone' dataKey='output_tokens' name='Output tokens' stroke='#47bfff' fill='url(#outputTokenFill)' strokeWidth={2} />
        </AreaChart>
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

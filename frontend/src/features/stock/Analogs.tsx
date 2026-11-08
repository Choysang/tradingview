import { useState } from 'react';
import { api } from '../../core/api';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { FanChart } from '../../kit/charts';
import { Seg, Skeleton } from '../../kit/primitives';

const HORIZONS = [{ value: '5', label: '5 日' }, { value: '10', label: '10 日' }, { value: '20', label: '20 日' }, { value: '40', label: '40 日' }];

/** What happened after the historical setups that look most like today's. */
export default function Analogs({ code }: { code: string }) {
  const [horizon, setHorizon] = useState('20');
  const forecast = useLive(signal => api.analogs(code, Number(horizon), signal), [code, horizon], false);
  const data = forecast.data;
  const last = data?.path[data.path.length - 1];

  return (
    <section className="sheet-block analogs">
      <div className="sheet-block-head">
        <h3>相似情景 <small className="faint">历史上与今天最像的样本，之后的走势分布</small></h3>
        <div className="sheet-controls">
          <Seg label="观察期" value={horizon} onChange={setHorizon} options={HORIZONS} />
        </div>
      </div>
      {forecast.error && !data && <p className="faint analogs-empty">暂无相似情景：{forecast.error}</p>}
      {!data && !forecast.error && <Skeleton height={220} radius={10} />}
      {data && last && (
        <>
          <dl className="analog-stats">
            <div><dt>上涨概率</dt><dd className={`num ${data.prob_up != null && data.prob_up >= 50 ? 'up' : 'down'}`}>{data.prob_up == null ? '—' : `${num(data.prob_up, 0)}%`}</dd></div>
            <div><dt>中位收益</dt><dd className={`num ${tone(last.p50)}`}>{pct(last.p50, 1)}</dd></div>
            <div><dt>平均收益</dt><dd className={`num ${tone(data.mean_return)}`}>{pct(data.mean_return, 1)}</dd></div>
            <div><dt>P10 ~ P90</dt><dd className="num">{pct(last.p10, 1)} ~ {pct(last.p90, 1)}</dd></div>
            <div><dt>样本</dt><dd className="num">{data.neighbors}</dd></div>
          </dl>
          <FanChart path={data.path} height={200} />
          {data.examples.length > 0 && (
            <div className="analog-examples">
              <span className="eyebrow">最相似的样本</span>
              <ul>
                {data.examples.map(example => (
                  <li key={`${example.code}-${example.date}`}>
                    <button onClick={() => useWorkspace.getState().openStock(example.code)}>
                      <span className="num">{example.code}</span>
                      <span className="num faint">{example.date}</span>
                      <span className={`num ${tone(example.forward_return)}`}>{pct(example.forward_return, 1)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <ul className="analog-notes">
            {data.notes.map(note => <li key={note}>{note}</li>)}
          </ul>
        </>
      )}
    </section>
  );
}

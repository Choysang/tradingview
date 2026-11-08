import { Plus, X } from 'lucide-react';
import { api } from '../../core/api';
import { money, num, pct } from '../../core/format';
import { useLive } from '../../core/live';
import { useMarket, useWorkspace } from '../../core/store';
import { FluidSpark } from '../../kit/charts';
import { Empty, Num, Panel, Skeleton } from '../../kit/primitives';
import { EventRow, useEventTape } from '../overview/EventTape';
import { Identity } from '../screener/ResultTable';
import { PageHead } from '../shell/Shell';
import './watchlist.css';

function WatchEvents({ codes }: { codes: string[] }) {
  const { events } = useEventTape();
  const mine = events.filter(event => codes.includes(event.code)).slice(0, 30);
  return (
    <Panel eyebrow="TAPE" title="自选异动" subtitle="只显示自选股的封板、炸板与急速异动">
      {mine.length ? <ol className="tape">{mine.map(event => <EventRow key={event.id} event={event} fresh={false} />)}</ol> : <p className="muted">自选股暂无异动。</p>}
    </Panel>
  );
}

/** An empty list is a dead end; offer today's strongest names as a starting point. */
function Suggestions() {
  const top = useLive(signal => api.query({ sort_by: 'composite_score', limit: 8 }, signal), [], false);
  const add = (code: string, name: string) => {
    useMarket.getState().toggleWatch(code)
      .then(() => useWorkspace.getState().notify(`已加入自选：${name}`, 'ok'))
      .catch(error => useWorkspace.getState().notify(`自选保存失败：${error.message}`, 'error'));
  };
  return (
    <div className="watch-empty">
      <Empty title="还没有自选股">按 <kbd>⌘</kbd><kbd>K</kbd> 搜索，或在选股表格、个股详情里点 ☆ 加入。</Empty>
      {top.data && (
        <section className="suggest">
          <span className="eyebrow">今日综合分靠前 · 一键加入</span>
          <div className="suggest-grid">
            {top.data.records.map(record => (
              <button key={record.code} className="suggest-card" onClick={() => add(record.code, record.name)}>
                <span><strong>{record.name}</strong><small className="num faint">{record.code}</small></span>
                <Identity record={record} />
                <span className={`num ${record.pct_chg >= 0 ? 'up' : 'down'}`}>{pct(record.pct_chg)}</span>
                <Plus size={14} className="suggest-plus" />
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export default function WatchlistView() {
  const codes = useMarket(state => state.watchlist);
  const quotes = useLive(signal => api.query({ codes, exclude_st: false, exclude_new: false, include_suspended: true, limit: 300 }, signal), [codes.join(',')]);
  const records = quotes.data?.records ?? [];
  const byCode = new Map(records.map(record => [record.code, record]));
  const ordered = codes.map(code => byCode.get(code)).filter(record => record != null);

  const remove = (code: string, name: string) => {
    useMarket.getState().toggleWatch(code)
      .then(() => useWorkspace.getState().notify(`已移出自选：${name}`, 'ok'))
      .catch(error => useWorkspace.getState().notify(`自选保存失败：${error.message}`, 'error'));
  };

  return (
    <div className="page-inner">
      <PageHead title="自选观察" lead="关注标的的实时状态与异动。在任意列表点 ☆ 或详情页加入。">
        <button className="btn" onClick={() => useWorkspace.setState({ commandOpen: true })}><Plus size={14} />添加股票</button>
      </PageHead>
      {!codes.length ? (
        <Suggestions />
      ) : (
        <div className="watch-layout">
          <div className="watch-grid">
            {!quotes.data && codes.map(code => <Skeleton key={code} height={168} radius={14} />)}
            {ordered.map((record, index) => (
              <article key={record.code} className={`watch-card ${record.pct_chg > 0 ? 'rise' : record.pct_chg < 0 ? 'fall' : ''}`} style={{ '--i': index } as React.CSSProperties}>
                <button className="watch-open" onClick={() => useWorkspace.getState().openStock(record.code)} aria-label={`打开${record.name}详情`} />
                <header>
                  <div>
                    <strong>{record.name}</strong>
                    <span className="num faint">{record.code}</span>
                  </div>
                  <button className="watch-remove" onClick={() => remove(record.code, record.name)} aria-label={`移出自选${record.name}`}><X size={13} /></button>
                </header>
                <div className="watch-price">
                  <Num value={record.price} format={value => num(value, 2)} toneOf={record.pct_chg} signed className="watch-last" />
                  <Num value={record.pct_chg} format={value => pct(value)} signed />
                </div>
                <FluidSpark values={record.intraday_sparkline} height={44} baseline={0} label={`${record.name}分时`} />
                <footer>
                  <Identity record={record} />
                  <span className="faint num">{money(record.amount_today)}</span>
                  {record.composite_score != null && <span className="watch-score num">{record.composite_score.toFixed(0)}</span>}
                </footer>
              </article>
            ))}
            {quotes.data && codes.filter(code => !byCode.has(code)).map(code => (
              <article key={code} className="watch-card missing">
                <header><div><strong className="num">{code}</strong><span className="faint">当前快照中没有该代码</span></div>
                  <button className="watch-remove" onClick={() => remove(code, code)} aria-label={`移出自选${code}`}><X size={13} /></button>
                </header>
              </article>
            ))}
          </div>
          <WatchEvents codes={codes} />
        </div>
      )}
    </div>
  );
}

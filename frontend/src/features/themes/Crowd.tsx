import { api } from '../../core/api';
import type { CrowdSnapshot, CrowdStock } from '../../core/contract';
import { num, pct, tone } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { ErrorNote, Panel, Skeleton } from '../../kit/primitives';

function Stock({ stock }: { stock: CrowdStock }) {
  return (
    <button className="crowd-stock" onClick={() => useWorkspace.getState().openStock(stock.code)} title={stock.code}>
      <span className="truncate">{stock.name}</span>
      {stock.is_limit_up && stock.limit_streak > 1 ? <i className="num">{stock.limit_streak}板</i> : null}
      <b className={`num ${tone(stock.pct_chg)}`}>{pct(stock.pct_chg, 1)}</b>
    </button>
  );
}

function Industries({ data }: { data: CrowdSnapshot }) {
  if (!data.industries.length) return <p className="faint">今天没有行业出现 2 只以上涨停。</p>;
  return (
    <ol className="crowd-list">
      {data.industries.map(item => (
        <li key={item.industry}>
          <header>
            <strong>{item.industry}</strong>
            <span className="num up">{item.limit_ups} 涨停</span>
            <small className="faint num">{item.members} 只 · 均 {pct(item.avg_pct, 1)}</small>
          </header>
          <div className="crowd-row">
            {item.leader && <span className="crowd-role">龙头</span>}
            {item.leader && <Stock stock={item.leader} />}
          </div>
          {item.followers.length > 0 && (
            <div className="crowd-row">
              <span className="crowd-role">跟风</span>
              {item.followers.map(stock => <Stock key={stock.code} stock={stock} />)}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Names({ data }: { data: CrowdSnapshot }) {
  if (!data.names.length) return <p className="faint">今天没有少见字的同名联动（至少 2 只涨停）。</p>;
  return (
    <ol className="crowd-list">
      {data.names.map(cluster => (
        <li key={cluster.char}>
          <header><strong className="crowd-char">{cluster.char}</strong><span className="num up">{cluster.limit_ups.length} 涨停</span></header>
          <div className="crowd-row">{cluster.limit_ups.map(stock => <Stock key={stock.code} stock={stock} />)}</div>
          {cluster.followers.length > 0 && (
            <div className="crowd-row"><span className="crowd-role">未涨停</span>{cluster.followers.map(stock => <Stock key={stock.code} stock={stock} />)}</div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Seats({ data }: { data: CrowdSnapshot }) {
  if (!data.lhb.length && !data.seats.length) return <p className="faint">没有龙虎榜数据。</p>;
  return (
    <div className="crowd-seats">
      {data.seats.length > 0 && (
        <table className="crowd-table">
          <thead><tr><th scope="col">今日上榜的买方席位</th><th scope="col">次数</th><th scope="col">战绩</th><th scope="col">一日游</th><th scope="col">今日买入</th></tr></thead>
          <tbody>
            {data.seats.map(seat => (
              <tr key={seat.seat}>
                <th scope="row" className="truncate" title={seat.seat}>
                  {seat.smart && <i className="seat-tag smart">强势</i>}{seat.trip && <i className="seat-tag trip">一日游</i>}{seat.seat}
                </th>
                <td className="num">{seat.events}</td>
                <td className={`num ${tone(seat.avg_return)}`}>{pct(seat.avg_return, 2)}</td>
                <td className="num">{seat.trip_share == null ? '—' : `${num(seat.trip_share, 0)}%`}</td>
                <td className="truncate">{seat.bought_today.join('、')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data.lhb.length > 0 && (
        <ol className="crowd-lhb">
          {data.lhb.map(item => (
            <li key={item.code}>
              <button onClick={() => useWorkspace.getState().openStock(item.code)}>
                <strong className="truncate">{item.name}</strong>
                <b className={`num ${tone(item.pct_chg)}`}>{pct(item.pct_chg, 1)}</b>
                <small className="num">强势席位 {item.smart_buyers}{item.trip_buyers ? ` · 一日游 ${item.trip_buyers}` : ''}</small>
                <small className={`num ${tone(item.inst_net)}`}>机构 {item.inst_net == null ? '—' : `${num(item.inst_net, 2)} 亿`}</small>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export default function Crowd() {
  const snapshot = useLive(signal => api.crowd(signal), [], false);
  const data = snapshot.data;
  return (
    <Panel eyebrow="CROWD" title="抱团与联动" subtitle={data ? `${data.trade_date} 收盘 · 行业扩散、同字联动与龙虎榜席位` : '行业扩散、同字联动与龙虎榜席位'} className="crowd-panel">
      {snapshot.error && !data && <ErrorNote>抱团快照加载失败：{snapshot.error}</ErrorNote>}
      {!data && !snapshot.error && <Skeleton height={220} radius={12} />}
      {data && (
        <>
          <div className="crowd-grid">
            <section><h4>行业扩散</h4><Industries data={data} /></section>
            <section><h4>同字联动</h4><Names data={data} /></section>
          </div>
          <section className="crowd-seat-block"><h4>龙虎榜席位</h4><Seats data={data} /></section>
          <ul className="assumptions">{data.notes.map(note => <li key={note}>{note}</li>)}</ul>
        </>
      )}
    </Panel>
  );
}

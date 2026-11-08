import { useEffect, useState } from 'react';
import { DownloadCloud, RefreshCw } from 'lucide-react';
import { api } from '../../core/api';
import type { WarehouseTable } from '../../core/contract';
import { num } from '../../core/format';
import { useLive } from '../../core/live';
import { useWorkspace } from '../../core/store';
import { Seg, Skeleton } from '../../kit/primitives';

const TABLE: Record<string, string> = { bars: '日线', index_bars: '指数', securities: '证券列表' };

function size(bytes: number) {
  return bytes >= 1e6 ? `${num(bytes / 1e6, 1)} MB` : `${num(bytes / 1e3, 0)} KB`;
}

function Tables({ tables }: { tables: WarehouseTable[] }) {
  if (!tables.length) return <p className="faint">空</p>;
  return (
    <ul className="wh-tables">
      {tables.map(table => (
        <li key={table.name}>
          <strong>{TABLE[table.name] ?? table.name}</strong>
          <span className="num">{num(table.rows, 0)} 行{table.codes != null ? ` · ${num(table.codes, 0)} 只` : ''}</span>
          <span className="num faint">{table.start && table.end ? `${table.start} → ${table.end}` : '—'}</span>
          <span className="num faint">{table.files} 个文件 · {size(table.bytes)}</span>
        </li>
      ))}
    </ul>
  );
}

/** The research history: what is stored, which set research runs on, and the sync controls. */
export default function WarehousePanel() {
  const status = useLive(signal => api.warehouse(signal), [], false);
  const [years, setYears] = useState('3');
  const [starting, setStarting] = useState(false);
  const job = status.data?.job;
  const running = job?.status === 'running';

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(status.refresh, 2000);
    return () => window.clearInterval(timer);
  }, [running, status.refresh]);

  const sync = async (kind: 'daily' | 'backfill') => {
    setStarting(true);
    try {
      await api.sync(kind === 'daily' ? { kind } : { kind, years: Number(years) });
      useWorkspace.getState().notify(kind === 'daily' ? '已开始增量同步' : `已开始回补 ${years} 年日线`, 'ok');
      status.refresh();
    } catch (error) {
      useWorkspace.getState().notify(`同步未开始：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setStarting(false);
    }
  };

  if (!status.data) return status.error ? <p className="error-note">{status.error}</p> : <Skeleton height={160} />;
  const data = status.data;
  return (
    <div className="warehouse">
      <div className={`wh-active ${data.active}`}>
        <div>
          <strong>{data.active === 'live' ? '研究使用真实历史' : '研究使用合成历史'}</strong>
          <p>{data.provenance.detail}</p>
          <p className="faint mono">{data.provenance.source} · 截至 {data.latest_trade_date ?? '—'} · {data.coverage_codes} 只</p>
        </div>
      </div>
      {job && (
        <div className={`wh-job ${job.status}`}>
          <div className="wh-job-head">
            <strong>{job.kind === 'backfill' ? '历史回补' : job.kind === 'daily' ? '增量同步' : '合成数据'} · {job.status === 'running' ? '进行中' : job.status === 'done' ? '已完成' : '失败'}</strong>
            <span className="num faint">{job.done}/{job.total}{job.errors ? ` · ${job.errors} 个错误` : ''}</span>
          </div>
          <span className="wh-progress"><i style={{ width: `${Math.round(job.progress * 100)}%` }} /></span>
          <p>{job.message}</p>
        </div>
      )}
      <div className="wh-actions">
        <button className="btn" onClick={() => sync('daily')} disabled={running || starting}><RefreshCw size={14} />增量同步</button>
        <Seg label="回补年数" value={years} onChange={setYears} options={[{ value: '1', label: '1 年' }, { value: '3', label: '3 年' }, { value: '5', label: '5 年' }]} />
        <button className="btn primary" onClick={() => sync('backfill')} disabled={running || starting}><DownloadCloud size={14} />回补全市场</button>
      </div>
      <div className="wh-datasets">
        <div><h4>真实历史 live</h4><Tables tables={data.datasets.live ?? []} /></div>
        <div><h4>合成历史 sim</h4><Tables tables={data.datasets.sim ?? []} /></div>
      </div>
      <ul className="wh-notes">
        {data.notes.map(note => <li key={note}>{note}</li>)}
      </ul>
    </div>
  );
}

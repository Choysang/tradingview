import { useEffect, useState } from 'react';
import { CheckCircle2, CircleDashed, X } from 'lucide-react';
import { api } from '../../core/api';
import { clock } from '../../core/format';
import { useLive } from '../../core/live';
import { useMarket, useWorkspace } from '../../core/store';
import { Seg, Skeleton } from '../../kit/primitives';
import WarehousePanel from './WarehousePanel';
import './settings.css';

/** Where every number comes from, how complete each field is, and the mode switch. */
export default function SourcesDialog() {
  const close = () => useWorkspace.setState({ sourcesOpen: false });
  const provenance = useMarket(state => state.overview?.provenance);
  const providers = useLive(signal => api.providers(signal), [], false);
  const quality = useLive(signal => api.quality(signal), []);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); close(); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const setMode = async (mode: 'LIVE' | 'SIM') => {
    setSwitching(true);
    try {
      const next = await api.setMode(mode);
      useWorkspace.getState().notify(next.mode === mode ? `已切换到 ${mode}` : `已请求 ${mode}，当前仍为 ${next.mode}：${next.detail}`, next.mode === mode ? 'ok' : undefined);
      providers.refresh();
    } catch (error) {
      useWorkspace.getState().notify(`切换失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    } finally {
      setSwitching(false);
    }
  };

  return (
    <>
      <div className="scrim" onClick={close} aria-hidden="true" />
      <div className="dialog sources" role="dialog" aria-modal="true" aria-labelledby="sources-title">
        <header className="sources-head">
          <div>
            <span className="eyebrow">DATA PROVENANCE</span>
            <h2 id="sources-title" className="display">数据来源</h2>
          </div>
          <button className="btn ghost icon" onClick={close} aria-label="关闭"><X size={16} /></button>
        </header>
        <div className="sources-body">
          <section className="sources-mode">
            <div>
              <strong>{provenance?.mode === 'LIVE' ? '实时行情' : '模拟行情'}</strong>
              <p>{provenance?.detail ?? '等待第一份快照'}</p>
              <p className="faint mono">{provenance?.source} · {clock(provenance?.as_of)} · 第 {provenance?.sequence ?? 0} 帧 · 覆盖 {provenance?.coverage ?? 0} 只</p>
            </div>
            <Seg<'LIVE' | 'SIM'>
              label="数据模式"
              value={(provenance?.mode ?? 'SIM') as 'LIVE' | 'SIM'}
              onChange={mode => { if (!switching) void setMode(mode); }}
              options={[{ value: 'SIM', label: 'SIM 模拟' }, { value: 'LIVE', label: 'LIVE 实时' }]}
            />
          </section>

          <section>
            <h3>数据边界</h3>
            {providers.data ? (
              <ul className="providers">
                {providers.data.providers.map(provider => (
                  <li key={provider.key} className={provider.active ? 'active' : ''}>
                    {provider.available ? <CheckCircle2 size={15} className="down" /> : <CircleDashed size={15} className="faint" />}
                    <div>
                      <strong>{provider.name}{provider.active && <span className="badge accent">在用</span>}</strong>
                      <p>{provider.detail}</p>
                    </div>
                    <span className="badge outline">{provider.kind}</span>
                  </li>
                ))}
              </ul>
            ) : <Skeleton height={120} />}
          </section>

          <section>
            <h3>历史数据仓 <small className="faint">回测、周期判定与每日推荐都读这里；真实历史不足时自动用合成历史并标注 SIM</small></h3>
            <WarehousePanel />
          </section>

          <section>
            <h3>字段覆盖率 <small className="faint">空值显示为 —，不参与对应筛选，不按 0 计算</small></h3>
            {quality.data ? (
              <ul className="coverage">
                {quality.data.fields.map(field => (
                  <li key={field.field}>
                    <span>{field.label}</span>
                    <span className="coverage-bar"><i style={{ width: `${field.coverage}%` }} className={field.coverage < 50 ? 'low' : field.coverage < 95 ? 'mid' : ''} /></span>
                    <b className="num">{field.coverage.toFixed(0)}%</b>
                  </li>
                ))}
              </ul>
            ) : <Skeleton height={160} />}
          </section>
          <p className="faint sources-foot">本工具仅用于研究与学习，不构成投资建议。SIM 模式下所有数字均为合成数据。</p>
        </div>
      </div>
    </>
  );
}

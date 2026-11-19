/**
 * Factor daily series (triage #37 #80): the daily rank IC and the cumulative long-short (top minus bottom quantile) curve of a factor experiment, per horizon
 * and per context slice (train / validation; the blind slice is never read). The series are computed by a queued job over the experiment's own sealed windows
 * (deterministic), so the page shows the state honestly: 尚未生成 -> 排队中 (until the worker `pulse.workers.fillr run` ran) -> the charts. A date without
 * an IC (cross-section too thin) is a gap in the line, never 0.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Badge, MissingText } from '../../../components/Badges';
import { EmptyState, ErrorState, Loading } from '../../../components/States';
import { useToast } from '../../../components/Toast';
import { explain } from '../../ext/b/api';
import type { FactorSeriesOne, FactorSeriesStatus } from './api';
import { bfFillr } from './api';
import { FACTOR_STATE_TEXT, linePath, numText, pctText, runStateText } from './format';
import './fillr.css';

const W = 640;
const H = 130;

export function FactorSeriesPanel({ factorId }: { factorId: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const key = ['bffr', 'factor-series', factorId];
  const q = useQuery({
    queryKey: key, queryFn: ({ signal }) => bfFillr.factorSeries({ factorId }, signal), retry: false,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.data.state ?? '') ? 5000 : false),
  });
  const queue = useMutation({
    mutationFn: (experimentId: string) => bfFillr.queueFactorSeries(experimentId),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: key }); toast.push('good', '已排队，等待后台任务运行'); },
    onError: (e) => toast.push('bad', `排队失败：${explain(e)}`),
  });
  if (q.isLoading) return <Loading label="读取逐日 IC 序列…" />;
  if (q.isError) return <ErrorState error={q.error} what="因子逐日序列" onRetry={() => q.refetch()} />;
  return <FactorSeriesView data={q.data!.data} onQueue={(id) => queue.mutate(id)} queueing={queue.isPending} />;
}

export function FactorSeriesView({ data, onQueue, queueing = false }: { data: FactorSeriesStatus; onQueue?: (experimentId: string) => void; queueing?: boolean }) {
  if (data.state === 'no_experiment') return <EmptyState>还没有已完成的因子实验，所以没有逐日序列（{data.reason ?? ''}）。</EmptyState>;
  if (data.state === 'ready' && data.series) {
    return (
      <div className="bffr factor-series" aria-label="逐日 IC 与多空曲线">
        <div className="fs-pair">{data.series.items.map((it) => <SeriesCard key={`${it.horizon}${it.slice}`} it={it} />)}</div>
        <p className="muted small">{data.series.computed_from}。逐日 IC 为 Spearman 秩相关；多空每隔 N 个交易日取样、不重叠复利；未扣成本、未考虑涨跌停。</p>
      </div>
    );
  }
  return (
    <div className="bffr queued-note" role="status">
      <Badge tone={data.state === 'failed' ? 'bad' : 'warn'}>{FACTOR_STATE_TEXT[data.state]}</Badge>{' '}
      {data.state === 'not_computed' && '逐日 IC 与多空曲线需要后台计算。'}
      {data.state === 'failed' && `上次计算失败（${data.job?.error_code ?? '未知'}），可以重新排队。`}
      {(data.state === 'queued' || data.state === 'running') && <>任务{runStateText(data.job?.run_state ?? data.state)}，由后台任务执行（<code>{data.worker_hint}</code>）；这个页面每几秒检查一次。</>}{' '}
      {(data.state === 'not_computed' || data.state === 'failed') && data.experiment_id && onQueue && (
        <button className="btn btn-on" disabled={queueing} onClick={() => onQueue(data.experiment_id as string)}>生成逐日序列</button>
      )}
    </div>
  );
}

function SeriesCard({ it }: { it: FactorSeriesOne }) {
  const ics = it.ic.map((p) => (p.ic == null ? null : Number(p.ic)));
  const known = ics.filter((v): v is number => v !== null);
  const bound = Math.max(0.05, ...known.map((v) => Math.abs(v)));
  const cum = it.long_short.map((p) => Number(p.cumulative));
  const cLo = Math.min(0, ...cum);
  const cHi = Math.max(0, ...cum);
  const yZero = 4 + (1 - (0 - -bound) / (2 * bound)) * (H - 8);
  const yCumZero = 4 + (1 - (0 - cLo) / ((cHi - cLo) || 1)) * (H - 8);
  const title = `未来 ${it.horizon} 日 · ${it.slice === 'train' ? '训练' : '验证'}`;
  return (
    <figure>
      <figcaption><b>{title}</b> <span className="muted small">IC 均值 {numText(it.ic_mean, 4)} · ICIR {numText(it.icir)} · 有效 {it.days} 日 · 多空 {pctText(it.long_short_return)}（{it.periods} 期）</span></figcaption>
      {it.ic.length === 0 ? <MissingText>没有 IC 日期</MissingText> : (
        <svg className="fs-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title} 逐日 IC，${it.ic.at(0)?.d ?? ''} 至 ${it.ic.at(-1)?.d ?? ''}`} preserveAspectRatio="none">
          <line x1="0" x2={W} y1={yZero} y2={yZero} className="grid" />
          <path d={linePath(ics, W, H, -bound, bound)} className="ic" />
        </svg>
      )}
      {it.long_short.length === 0 ? <p className="muted small"><MissingText>没有多空曲线</MissingText>（截面太薄或窗口太短）</p> : (
        <svg className="fs-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title} 多空累计曲线，终值 ${pctText(it.long_short.at(-1)?.cumulative)}`} preserveAspectRatio="none">
          <line x1="0" x2={W} y1={yCumZero} y2={yCumZero} className="grid" />
          <path d={linePath(cum, W, H, cLo, cHi)} className="ls" />
        </svg>
      )}
      <p className="muted small">上：逐日 rank IC（虚线为 0）；下：多空（高分位 - 低分位）累计收益。</p>
    </figure>
  );
}

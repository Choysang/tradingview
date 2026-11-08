import type { ReactNode } from 'react';
import type { FilterInput } from '../../core/api';
import { DEFAULT_FILTER } from '../../core/contract';
import type { BoardType, MacroPoolType, TurnoverRegime } from '../../core/contract';
import { NONE, useMarket } from '../../core/store';
import { Range, Seg, Toggle } from '../../kit/primitives';

const STREAKS = [1, 2, 3, 4, 5, 6, 7];
const TOP_STREAK = 20;
const SEAL_TIMES: { value: string; label: string }[] = [
  { value: '', label: '不限' },
  { value: '09:25:00', label: '一字' },
  { value: '09:35:00', label: '09:35' },
  { value: '10:00:00', label: '10:00' },
  { value: '11:30:00', label: '午前' },
];

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="rail-section">
      <legend>{title}</legend>
      {children}
    </fieldset>
  );
}

function Slider({ label, min, max, step = 1, value, format, onChange }: { label: string; min: number; max: number; step?: number; value: number; format: (value: number) => string; onChange: (value: number) => void }) {
  const ratio = ((value - min) / (max - min || 1)) * 100;
  return (
    <div className="range">
      <div className="range-head"><span>{label}</span><output>{format(value)}</output></div>
      <div className="range-track">
        <span className="range-fill" style={{ left: 0, width: `${ratio}%` }} />
        <input type="range" aria-label={label} min={min} max={max} step={step} value={value} onChange={event => onChange(Number(event.target.value))} />
      </div>
    </div>
  );
}

interface FilterRailProps {
  filters: FilterInput;
  onChange: (patch: FilterInput) => void;
}

/** Every control writes one contract field; nothing here computes a result. */
export default function FilterRail({ filters, onChange }: FilterRailProps) {
  const f = { ...DEFAULT_FILTER, ...filters };
  const themes = useMarket(state => state.overview?.hot_themes ?? NONE);
  const streakSet = new Set(f.limit_streak_exact ?? []);

  const toggleBoard = (board: BoardType) => {
    const next = f.boards.includes(board) ? f.boards.filter(item => item !== board) : [...f.boards, board];
    onChange({ boards: next.length ? next : DEFAULT_FILTER.boards });
  };
  const toggleStreak = (level: number) => {
    const values = level === 7 ? Array.from({ length: TOP_STREAK - 6 }, (_, index) => index + 7) : [level];
    const has = values.every(value => streakSet.has(value));
    const next = new Set(streakSet);
    values.forEach(value => (has ? next.delete(value) : next.add(value)));
    const list = [...next].sort((a, b) => a - b);
    onChange({ limit_streak_exact: list.length ? list : null, pool_type: list.length && f.pool_type === 'all' ? 'limit_up' : f.pool_type });
  };
  const toggleTheme = (theme: string) => {
    const next = f.selected_themes.includes(theme) ? f.selected_themes.filter(item => item !== theme) : [...f.selected_themes, theme];
    onChange({ selected_themes: next });
  };

  return (
    <div className="filter-rail">
      <Section title="候选池">
        <Seg<MacroPoolType>
          full
          label="候选池"
          value={f.pool_type}
          onChange={pool_type => onChange({ pool_type })}
          options={[{ value: 'all', label: '全部' }, { value: 'limit_up', label: '涨停' }, { value: 'broken_attack', label: '炸板' }, { value: 'n_days_m_boards', label: '反包' }, { value: 'growth_streak', label: '连阳' }]}
        />
      </Section>

      <Section title="连板高度">
        <div className="chip-row">
          {STREAKS.map(level => {
            const active = level === 7 ? streakSet.has(7) : streakSet.has(level);
            return <button key={level} className="chip" aria-pressed={active} onClick={() => toggleStreak(level)}>{level === 1 ? '首板' : level === 7 ? '7板+' : `${level}板`}</button>;
          })}
        </div>
      </Section>

      <Section title="连阳">
        <div className="chip-row">
          {[3, 4, 5, 6, 7].map(level => (
            <button key={level} className="chip" aria-pressed={f.min_growth_streak === level} onClick={() => onChange({ min_growth_streak: f.min_growth_streak === level ? null : level })}>≥{level}天</button>
          ))}
        </div>
        <Toggle checked={f.allow_doji_tolerance} onChange={allow_doji_tolerance => onChange({ allow_doji_tolerance })}>假阴容错（涨幅 ≥ −0.4% 且缩量）</Toggle>
      </Section>

      <Section title="区间">
        <Range
          label="自由流通市值"
          min={0}
          max={1000}
          step={10}
          low={f.free_cap_min}
          high={Math.min(f.free_cap_max, 1000)}
          format={(low, high) => `${low}–${high >= 1000 ? '∞' : high} 亿`}
          onChange={(low, high) => onChange({ free_cap_min: low, free_cap_max: high >= 1000 ? DEFAULT_FILTER.free_cap_max : high })}
        />
        <Range
          label="涨幅"
          min={-10}
          max={20}
          step={0.5}
          low={f.pct_chg_min ?? -10}
          high={f.pct_chg_max ?? 20}
          format={(low, high) => `${low <= -10 ? '−∞' : `${low}%`} – ${high >= 20 ? '∞' : `${high}%`}`}
          onChange={(low, high) => onChange({ pct_chg_min: low <= -10 ? null : low, pct_chg_max: high >= 20 ? null : high })}
        />
        <Range
          label="今日换手"
          min={0}
          max={50}
          step={0.5}
          low={f.turnover_today_min}
          high={Math.min(f.turnover_today_max, 50)}
          format={(low, high) => `${low}–${high >= 50 ? '∞' : high}%`}
          onChange={(low, high) => onChange({ turnover_today_min: low, turnover_today_max: high >= 50 ? 100 : high })}
        />
        <Range
          label="股价"
          min={0}
          max={200}
          step={1}
          low={f.price_min}
          high={Math.min(f.price_max, 200)}
          format={(low, high) => `${low}–${high >= 200 ? '∞' : high} 元`}
          onChange={(low, high) => onChange({ price_min: low, price_max: high >= 200 ? DEFAULT_FILTER.price_max : high })}
        />
        <Slider label="成交额下限" min={0} max={50} step={0.5} value={f.min_amount_today} format={value => (value ? `${value} 亿` : '不限')} onChange={min_amount_today => onChange({ min_amount_today })} />
      </Section>

      <Section title="量能结构">
        <Seg<TurnoverRegime>
          full
          label="昨换比结构"
          value={f.turnover_regime}
          onChange={turnover_regime => onChange({ turnover_regime })}
          options={[{ value: 'all', label: '全部' }, { value: 'shrink', label: '缩量' }, { value: 'normal', label: '平量' }, { value: 'expand', label: '放量' }]}
        />
      </Section>

      <Section title="封板质量">
        <Seg<string>
          full
          label="首次封板不晚于"
          value={f.max_first_seal_time ?? ''}
          onChange={value => onChange({ max_first_seal_time: value || null })}
          options={SEAL_TIMES}
        />
        <Slider label="封成比下限" min={0} max={50} step={1} value={f.min_seal_to_amount_ratio} format={value => (value ? `${value}%` : '不限')} onChange={min_seal_to_amount_ratio => onChange({ min_seal_to_amount_ratio })} />
        <Toggle checked={f.max_open_board_count === 0} onChange={hard => onChange({ max_open_board_count: hard ? 0 : null })}>只看硬板（未开板）</Toggle>
      </Section>

      <Section title="股性与位置">
        <Slider label="近一年涨停" min={0} max={20} value={f.min_zt_count_1y} format={value => (value ? `≥ ${value} 次` : '不限')} onChange={min_zt_count_1y => onChange({ min_zt_count_1y })} />
        <Toggle checked={f.only_near_60d_high} onChange={only_near_60d_high => onChange({ only_near_60d_high })}>临近或突破 60 日新高</Toggle>
      </Section>

      <Section title="制式与范围">
        <div className="chip-row">
          {(['10cm', '20cm', '30cm'] as BoardType[]).map(board => (
            <button key={board} className="chip" aria-pressed={f.boards.includes(board) && f.boards.length < 3} onClick={() => toggleBoard(board)}>
              {{ '10cm': '主板 10cm', '20cm': '双创 20cm', '30cm': '北交 30cm' }[board]}
            </button>
          ))}
        </div>
        <Toggle checked={f.exclude_st} onChange={exclude_st => onChange({ exclude_st })}>剔除 ST</Toggle>
        <Toggle checked={f.exclude_new} onChange={exclude_new => onChange({ exclude_new })}>剔除次新</Toggle>
      </Section>

      {themes.length > 0 && (
        <Section title="题材">
          <div className="chip-row">
            {themes.slice(0, 14).map(theme => (
              <button key={theme.theme_name} className="chip" aria-pressed={f.selected_themes.includes(theme.theme_name)} onClick={() => toggleTheme(theme.theme_name)}>
                {theme.theme_name}<span className="count">{theme.limit_up_count}</span>
              </button>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

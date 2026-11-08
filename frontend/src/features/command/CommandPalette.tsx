import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowRight, Database, Radio, Rows3, Search, Sparkles, TrendingUp } from 'lucide-react';
import { navigate } from '../../app/router';
import { VIEWS } from '../../app/views';
import { api, isAbort } from '../../core/api';
import type { FilterInput } from '../../core/api';
import type { StockTickRecord } from '../../core/contract';
import { pct, tone } from '../../core/format';
import { useDebounced } from '../../core/live';
import { useMarket, useWorkspace } from '../../core/store';
import './command.css';

interface Item {
  id: string;
  group: string;
  label: ReactNode;
  hint?: ReactNode;
  icon: ReactNode;
  run: () => void | Promise<void>;
}

async function askAndScreen(text: string) {
  const result = await api.parse(text);
  useWorkspace.getState().replaceFilters(result.filters as FilterInput, text);
  navigate('screener');
}

export default function CommandPalette() {
  const open = useWorkspace(state => state.commandOpen);
  const [text, setText] = useState('');
  const [stocks, setStocks] = useState<StockTickRecord[]>([]);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const query = useDebounced(text.trim(), 120);
  const mode = useMarket(state => state.overview?.provenance.mode);

  const close = () => useWorkspace.setState({ commandOpen: false });

  useEffect(() => {
    if (!open) return;
    setText('');
    setActive(0);
    window.setTimeout(() => input.current?.focus(), 10);
  }, [open]);

  useEffect(() => {
    if (!open || !query) { setStocks([]); return; }
    const controller = new AbortController();
    api.query({ search: query, limit: 8, exclude_st: false, exclude_new: false, sort_by: 'amount_today' }, controller.signal)
      .then(result => setStocks(result.records))
      .catch(error => { if (!isAbort(error)) setStocks([]); });
    return () => controller.abort();
  }, [open, query]);

  const items = useMemo<Item[]>(() => {
    const out: Item[] = [];
    const needle = text.trim().toLowerCase();
    stocks.forEach(stock => out.push({
      id: `s-${stock.code}`,
      group: '股票',
      icon: <TrendingUp size={15} />,
      label: <><b>{stock.name}</b><span className="num faint">{stock.code}</span></>,
      hint: <span className={`num ${tone(stock.pct_chg)}`}>{pct(stock.pct_chg)}</span>,
      run: () => useWorkspace.getState().openStock(stock.code),
    }));
    if (needle.length >= 2 && !/^\d+$/.test(needle)) {
      out.push({
        id: 'ask',
        group: '问句选股',
        icon: <Sparkles size={15} />,
        label: <>用问句筛选：<b>{text.trim()}</b></>,
        hint: <ArrowRight size={14} />,
        run: () => askAndScreen(text.trim()).catch(error => useWorkspace.getState().notify(`问句解析失败：${error.message}`, 'error')),
      });
    }
    VIEWS.filter(view => !needle || view.label.includes(needle) || view.code.toLowerCase().includes(needle) || view.description.includes(needle)).forEach((view, index) => out.push({
      id: `v-${view.id}`,
      group: '跳转',
      icon: <view.icon size={15} />,
      label: view.label,
      hint: <kbd>{VIEWS.indexOf(view) + 1}</kbd>,
      run: () => navigate(view.id),
    }));
    const actions: Item[] = [
      {
        id: 'mode',
        group: '操作',
        icon: <Radio size={15} />,
        label: mode === 'LIVE' ? '切换到模拟行情（SIM）' : '尝试接入实时行情（LIVE）',
        run: async () => {
          const next = mode === 'LIVE' ? 'SIM' : 'LIVE';
          const provenance = await api.setMode(next);
          useWorkspace.getState().notify(provenance.mode === next ? `已切换到 ${next}` : `已请求 ${next}：${provenance.detail}`, provenance.mode === next ? 'ok' : undefined);
        },
      },
      { id: 'density', group: '操作', icon: <Rows3 size={15} />, label: '切换表格行高', run: () => { useWorkspace.setState(state => ({ density: state.density === 'compact' ? 'comfortable' : 'compact' })); } },
      { id: 'sources', group: '操作', icon: <Database size={15} />, label: '数据来源与字段质量', run: () => { useWorkspace.setState({ sourcesOpen: true }); } },
    ];
    actions.filter(action => !needle || String(action.label).includes(needle)).forEach(action => out.push(action));
    return out;
  }, [stocks, text, mode]);

  useEffect(() => setActive(0), [items.length]);
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  if (!open) return null;

  const run = (item: Item | undefined) => {
    if (!item) return;
    close();
    void item.run();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive(value => Math.min(items.length - 1, value + 1)); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive(value => Math.max(0, value - 1)); }
    else if (event.key === 'Enter') { event.preventDefault(); run(items[active]); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
  };

  let lastGroup = '';
  return (
    <>
      <div className="scrim" onClick={close} aria-hidden="true" />
      <div className="dialog palette" role="dialog" aria-modal="true" aria-label="命令面板" onKeyDown={onKeyDown}>
        <div className="palette-input">
          <Search size={17} />
          <input
            ref={input}
            value={text}
            onChange={event => setText(event.target.value)}
            placeholder="股票代码、名称，或一句选股问句……"
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={items[active] ? `pi-${items[active].id}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd>Esc</kbd>
        </div>
        <div className="palette-list" id="palette-list" role="listbox" ref={list}>
          {items.map((item, index) => {
            const header = item.group !== lastGroup ? item.group : null;
            lastGroup = item.group;
            return (
              <div key={item.id}>
                {header && <div className="palette-group">{header}</div>}
                <div
                  id={`pi-${item.id}`}
                  role="option"
                  aria-selected={index === active}
                  className="palette-item"
                  onPointerMove={() => setActive(index)}
                  onClick={() => run(item)}
                >
                  <span className="palette-icon">{item.icon}</span>
                  <span className="palette-label">{item.label}</span>
                  {item.hint && <span className="palette-hint">{item.hint}</span>}
                </div>
              </div>
            );
          })}
          {!items.length && <p className="palette-empty">没有匹配项</p>}
        </div>
        <footer className="palette-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 执行</span>
          <span><kbd>/</kbd> 直接提问</span>
        </footer>
      </div>
    </>
  );
}

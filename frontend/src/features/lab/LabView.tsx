import { useWorkspace } from '../../core/store';
import { Seg } from '../../kit/primitives';
import { PageHead } from '../shell/Shell';
import BacktestPanel from './BacktestPanel';
import FactorPanel from './FactorPanel';
import './lab.css';

export default function LabView() {
  const tab = useWorkspace(state => state.labTab);
  return (
    <div className="page-inner">
      <PageHead
        title="策略实验室"
        lead={tab === 'backtest'
          ? '事件驱动回测：信号用已完成日线，次日开盘成交，成本与涨跌停约束逐条写明。'
          : '横截面因子检验：秩 IC、分组收益与多空曲线，回答「这个因子有没有预测力」。'}
      >
        <Seg<'backtest' | 'factor'>
          label="实验类型"
          value={tab}
          onChange={labTab => useWorkspace.setState({ labTab })}
          options={[{ value: 'backtest', label: '策略回测' }, { value: 'factor', label: '因子检验' }]}
        />
      </PageHead>
      {tab === 'backtest' ? <BacktestPanel /> : <FactorPanel />}
    </div>
  );
}

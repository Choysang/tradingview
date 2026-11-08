import { ArrowRight } from 'lucide-react';
import type { LadderMember, LadderTierStat, MacroDashboardSummary } from '../../core/contract';
import { hm, pct } from '../../core/format';
import { useMarket, useWorkspace } from '../../core/store';
import { Panel } from '../../kit/primitives';
import { screen } from './OverviewView';

function Member({ member, top }: { member: LadderMember; top: boolean }) {
  const watched = useMarket(state => state.watchlist.includes(member.code));
  const broken = !member.is_limit_up;
  const tip = [member.first_seal_time ? `${hm(member.first_seal_time)} 首封` : null, member.theme, pct(member.pct_chg)].filter(Boolean).join(' · ');
  return (
    <li>
      <button
        className={`rung-chip ${broken ? 'broken' : 'sealed'} ${top ? 'top' : ''} ${watched ? 'watched' : ''}`}
        onClick={() => useWorkspace.getState().openStock(member.code)}
        data-tip={tip}
        aria-label={`${member.name} ${broken ? '已开板' : '封板'} ${tip}`}
      >
        <i className="rung-dot" aria-hidden="true" />
        <span className="rung-name">{member.name}</span>
        <span className="rung-time num">{hm(member.first_seal_time)}</span>
        {(member.open_board_count ?? 0) > 0 && <span className="rung-break num">炸{member.open_board_count}</span>}
      </button>
    </li>
  );
}

function Rung({ tier, level, maxLevel, index }: { tier: LadderTierStat; level: number; maxLevel: number; index: number }) {
  const top = level === maxLevel && level > 1;
  const members = tier.members;
  const hidden = tier.count - members.length;
  return (
    <div className={`rung ${top ? 'is-top' : ''}`} style={{ '--step': level / Math.max(maxLevel, 1), '--i': index } as React.CSSProperties}>
      <button className="rung-head" onClick={() => screen({ pool_type: 'limit_up', limit_streak_exact: [level] }, `${level}连板`)} aria-label={`查看 ${level} 连板 ${tier.count} 家`}>
        <span className="rung-step" aria-hidden="true" />
        <span className="rung-level num">{level}<small>板</small></span>
        <span className="rung-meta">
          <span><b className="num">{tier.count}</b> 家</span>
          {tier.promotion_rate != null && <em className="num">晋级 {tier.promotion_rate.toFixed(0)}%</em>}
        </span>
      </button>
      <ul className="rung-members">
        {members.map(member => <Member key={member.code} member={member} top={top} />)}
        {!members.length && tier.top_stock_name && <li className="rung-fallback">{tier.top_stock_name} 等 {tier.count} 家</li>}
        {hidden > 0 && members.length > 0 && <li className="rung-more num">+{hidden}</li>}
      </ul>
    </div>
  );
}

/** 连板天梯: each rung is a tier, its step length is its height — the board reads as a staircase. */
export default function Ladder({ overview }: { overview: MacroDashboardSummary }) {
  const tiers = [...overview.limit_ladder_tiers]
    .map(tier => ({ tier, level: Number.parseInt(tier.tier_key, 10) }))
    .filter(item => Number.isFinite(item.level))
    .sort((a, b) => b.level - a.level);
  const maxLevel = tiers[0]?.level ?? 1;
  return (
    <Panel
      eyebrow="LADDER"
      title="连板天梯"
      subtitle="晋级率 = 昨日同梯队今日继续封板的比例；实心为封板，空心为已开板"
      actions={<button className="btn sm ghost" onClick={() => screen({ pool_type: 'limit_up' }, '涨停')}>全部涨停<ArrowRight size={13} /></button>}
    >
      {tiers.length ? (
        <div className="ladder">
          {tiers.map(({ tier, level }, index) => <Rung key={tier.tier_key} tier={tier} level={level} maxLevel={maxLevel} index={index} />)}
        </div>
      ) : (
        <p className="muted">今日暂无连板梯队。</p>
      )}
    </Panel>
  );
}

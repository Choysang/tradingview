import { TRI_LABEL, type EvalNode, type Tri } from '../lib/condition';

const ICON: Record<Tri, string> = { TRUE: '✓', FALSE: '✗', UNKNOWN: '？' };

export function TriBadge({ tri }: { tri: Tri }) {
  return (
    <span className={`tri tri-${tri.toLowerCase()}`} title={`${tri}：${TRI_LABEL[tri]}`}>
      <span aria-hidden="true">{ICON[tri]}</span> {TRI_LABEL[tri]} <span className="mono tri-en">{tri}</span>
    </span>
  );
}

export function ConditionTree({ node }: { node: EvalNode }) {
  return (
    <ul className="cond-tree">
      <ConditionNode node={node} />
    </ul>
  );
}

function ConditionNode({ node }: { node: EvalNode }) {
  return (
    <li className={`cond cond-${node.tri.toLowerCase()}`}>
      <div className="cond-head">
        <TriBadge tri={node.tri} />
        <span className="cond-label">{node.label}</span>
      </div>
      {node.tri === 'UNKNOWN' && node.children.length === 0 && <div className="cond-detail small">{node.detail}</div>}
      {node.tri !== 'UNKNOWN' && node.children.length === 0 && <div className="cond-detail muted small">{node.detail}</div>}
      {node.children.length > 0 && (
        <ul>
          {node.children.map((c, i) => <ConditionNode key={`${i}-${c.label}`} node={c} />)}
        </ul>
      )}
    </li>
  );
}

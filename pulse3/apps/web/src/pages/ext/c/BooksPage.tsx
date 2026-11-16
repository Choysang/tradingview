/** 前向主书: forward-tracking books on top, the forward monitor of the published rankings, the user's authorized accounts below. */
import { ForwardBooks } from './ForwardBooks';
import { ForwardMonitor } from './ForwardMonitor';
import { MyAccounts } from './MyAccounts';

export function BooksPage() {
  return (
    <div className="page">
      <ForwardBooks />
      <ForwardMonitor />
      <MyAccounts />
    </div>
  );
}

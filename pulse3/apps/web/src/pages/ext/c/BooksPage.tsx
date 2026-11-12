/** 前向主书: forward-tracking books on top, the user's authorized accounts below. */
import { ForwardBooks } from './ForwardBooks';
import { MyAccounts } from './MyAccounts';

export function BooksPage() {
  return (
    <div className="page">
      <ForwardBooks />
      <MyAccounts />
    </div>
  );
}

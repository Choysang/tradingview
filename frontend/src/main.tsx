import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './kit/base.css';
import './kit/tokens.css';
import './kit/kit.css';
import App from './app/App';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

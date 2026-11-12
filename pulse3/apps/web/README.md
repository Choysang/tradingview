# pulse3 web (W08)

React 19 + TypeScript + Vite 8 + TanStack Query + React Router, charts by lightweight-charts 5.3.0.
The browser only talks to the 31 operations of `src/pulse/contracts/data/openapi.json` under `/v1`.
Types are generated from that file; there are no hand-written DTOs. All versions are pinned exactly in `package.json`.

## Commands (run in `apps/web`)

| Command | What it does |
| --- | --- |
| `pnpm install` | install pinned dependencies |
| `pnpm gen:api` | regenerate `src/api/schema.d.ts` from `../../src/pulse/contracts/data/openapi.json` (openapi-typescript) |
| `pnpm dev` | real mode: Vite dev server on :5173, `/v1` proxied to `VITE_API_PROXY_TARGET` (default `http://127.0.0.1:8200`) |
| `pnpm dev:mock` | synthetic mode (`.env.mock`): contract-valid mock served by a Vite middleware; a "合成样本" banner is always shown |
| `pnpm build` | `tsc --noEmit` then `vite build` (real mode) |
| `pnpm build:mock` / `pnpm preview:mock` | build / preview the bundle with the mock middleware |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm test` | Vitest (pagination pinning, three-valued conditions, update cursor, stale display, mock semantics) |
| `pnpm mock:dump` | write every mock response to `mock/out/responses.json` |
| `pnpm mock:validate` | dump, then validate each response against the contract with `pulse.contracts.check` (needs `uv`; exit 0 only if all 31 operations are covered and valid) |

## Real vs mock

* Real mode never loads mock code; the mock is a Vite plugin (`mock/plugin.ts`) enabled only when `VITE_USE_MOCK=1`.
* Mock mode serves `/v1/*` plus `/__mock/*` dev switches (not part of the contract): `publish` (new release + `decision.published`), `glitch` (duplicate and older-version events), `expire` (stream epoch change / cursor expiry), `stale` (toggle stale data), `account`. The banner has buttons for them.
* The banner also appears whenever any real response carries the `SYNTHETIC_FIXTURE` warning.

## Real API (default) and production serving

`pnpm dev` proxies `/v1` and `/app-config.json` to `VITE_API_PROXY_TARGET` (default `http://127.0.0.1:8200`). Start the API with
`PULSE_DSN=postgresql://postgres@127.0.0.1:55432/pulse PULSE_ENV=dev PULSE_DEV_PRINCIPAL=<uuid> uv run uvicorn pulse.api.app:app --port 8200`.
`pnpm build` writes `apps/web/dist`; FastAPI serves it (SPA fallback) from the same port, so production is one process.
At boot `main.tsx` fetches `/app-config.json` (not a contract operation) before the app modules load: `slot_key` (env `PULSE_SLOT_KEY`,
default `main`), horizons and sector taxonomies read from the current release of that slot (defaults when none), `quote_policy_version`,
`session_policy_version`. `PULSE_APP_CONFIG_FILE` (JSON) overrides any key. Candidate-pool ids are never configured: they come from the
release component `candidate_pool:<id>`.

## Client config (values the contract needs as inputs but does not enumerate)

Precedence: `VITE_*` env > `/app-config.json` > built-in default.

`src/config.ts`, overridable by env (`.env.example`): `VITE_SLOT_KEY`, `VITE_HORIZONS` (short/swing/long -> horizon_id + forecast_target), `VITE_TAXONOMIES`, `VITE_ACCOUNT_ID` / `VITE_ACCOUNT_VERSION` / `VITE_VALUATION_SNAPSHOT_ID`, `VITE_LEAGUE_ID` / `VITE_LEAGUE_VERSION`, `VITE_LAYOUT_ID`, `VITE_QUOTE_POLICY_VERSION`. The account and league pointers can also be given in the URL (`?account=&av=&vs=`, `?league=&lv=`).

## Layout

* `src/api` generated schema, typed client, one function per operationId (`ops.ts`), query keys.
* `src/lib` pure rules: decimal compare/format, pinned pagination, update cursor, three-valued conditions, version/stale checks, evidence grade mapping, bars parameters.
* `src/hooks` release follow/pin, update polling, user state (layout / watchlist / annotations via API), pinned pages.
* `src/pages` Home, Sector, Stock workbench, Strategies & league, Papers, Data status, Portfolio.
* `mock/` synthetic server. `tests/` Vitest. `docs/web/M4_evidence.md` (repo root) maps page fields to operationId + response path and lists contract gaps.

## Rules the UI keeps

* Lists pin `ranking_id` / `release_id` (`actionability_id` for account lists); "load more" continues the same list and never re-sorts.
* A block answering for another release is hidden or flagged stale, never mixed into the page.
* Missing data reads 缺失 / UNKNOWN; it is never shown as 0. Indicators are never computed in the browser.
* The live quote card is separate from the frozen analysis.
* Persistent state (layouts, watchlists, notes, drawings) goes through the API only; TanStack Query is a display cache. The only localStorage use is the light/dark preference.

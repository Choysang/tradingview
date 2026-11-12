-- Extension group A (market state, ranking history / track record, sector details, sentiment, streams).
-- Why: the derived datasets pulse.market_today (D01), pulse.market_sentiment (D11) and pulse.ranking_outcome (D02)
-- are new manifest datasets (no new fact table, per the product extension cards); their contract ids must exist in
-- core.contract_version before a manifest can reference them. pulse.market_tape is the internal payload contract
-- of the tape event log (ops.outbox aggregate market_tape). The three supplementary read operations are registered
-- here so that every served path has a binding (cards: "proposed operation_id先登记core.api_binding").
-- Idempotent and additive; no table is created or altered.
BEGIN;

INSERT INTO core.contract_version(contract_id, schema_version, schema_document, schema_hash, compatibility_policy, created_at)
SELECT c.id, '3.0.0',
       jsonb_build_object('type', 'object', 'description', c.descr, 'x-owner', 'extension-group-A'),
       encode(sha256(convert_to(c.id || '|' || c.descr, 'UTF8')), 'hex'),
       'additive-optional-v1', now()
FROM (VALUES
  ('pulse.market_today',     'Panels of the market overview per release (thermometer, vitals, ladder, seal_clock, breadth, themes, growth_strip, regime); card PEX01 / D01.'),
  ('pulse.market_sentiment', 'Sentiment features per scope (market or sector) per release; card PEX09 / D11.'),
  ('pulse.ranking_outcome',  'Forward outcome of an immutable ranking under a fixed evaluation method; card PEX02 / D02.'),
  ('pulse.market_tape',      'Event tape payload (seal, reseal, break, surge, plunge, new_high) derived from consecutive quote snapshots.')
) AS c(id, descr)
ON CONFLICT (contract_id, schema_version) DO NOTHING;

INSERT INTO core.api_binding(operation_id, http_method, path_template, response_contract_id, response_schema_version,
                             application_service, read_model, freshness_policy_version)
VALUES
  ('readArtifactA',      'GET', '/v1/x/a/artifact',            'pulse.market_today', '3.0.0', 'market_today_service', 'market.feature_result,market.dataset_manifest', 'ext-a-v1'),
  ('queryMarketTape',    'GET', '/v1/x/a/market-tape',         'pulse.market_tape',  '3.0.0', 'market_today_service', 'ops.outbox',                                    'ext-a-v1'),
  ('streamMarketTape',   'GET', '/v1/x/a/market-tape/stream',  'pulse.market_tape',  '3.0.0', 'market_today_service', 'ops.outbox',                                    'ext-a-v1')
ON CONFLICT (operation_id) DO NOTHING;

COMMIT;

"""Synthetic v3 contract → linked views → ONE ledger → evaluation conformance.

No supplier call, strategy fitting, condition interpreter, matcher, PostgreSQL or
broker is implemented here. Fills come from the existing hand-written B01 fixture.
All business DTOs use the authoritative v3 schema; the economic engine is imported.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator, FormatChecker
from unified_ledger import Ledger, POLICY_VERSION, NUMERIC_POLICY_VERSION, replay_fixture, true_time_weighted_return

HERE = Path(__file__).resolve().parent
ARTIFACT_FILE = 'artifacts.json'
RESULT_FILE = 'result.json'


def uid(name):
    return str(uuid5(NAMESPACE_URL, 'urn:pulse:synthetic-vertical-v3:' + name))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text_decimal(value):
    number = Decimal(value)
    return '0' if number.is_zero() else format(number, 'f')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema', type=Path, default=HERE.parent / 'contracts' / 'v3.schema.json')
    parser.add_argument('--out-dir', type=Path, default=HERE / 'fixtures')
    args = parser.parse_args()
    schema_bytes = args.schema.read_bytes()
    schema = json.loads(schema_bytes)
    Draft202012Validator.check_schema(schema)
    checker = FormatChecker()
    validated = []

    def validate(name, value):
        root = dict(schema)
        root['$ref'] = '#/$defs/' + name
        Draft202012Validator(root, format_checker=checker).validate(value)
        validated.append(name)

    fixtures = json.loads((HERE / 'golden_fixtures.json').read_text())
    original = next(c for c in fixtures['cases'] if c['case_id'] == 'B01')
    case = deepcopy(original)
    instrument_id, release_id, account_id = uid('instrument'), uid('release'), uid('account')
    plan_ref = {'plan_id': uid('plan'), 'plan_version': 1}
    strategy_ref = {'instance_id': uid('instance'), 'strategy_id': uid('strategy'), 'strategy_version': 1}
    taxonomy = {'taxonomy_id': 'fixture.industry', 'taxonomy_version': 1, 'node_id': 'synthetic_sector'}
    old_symbol = next(iter(case['marks']))
    case['marks'] = {instrument_id: case['marks'][old_symbol]}
    case['initial']['positions'] = {
        instrument_id if k == old_symbol else k: v for k, v in case['initial']['positions'].items()}
    for event in case['events']:
        if event['payload'].get('instrument_id') == old_symbol:
            event['payload']['instrument_id'] = instrument_id

    # The existing reducer alone checks the reservation and all economic events.
    initial_book = Ledger(**case['initial'])
    probe = Ledger(**case['initial'])
    probe.apply(case['events'][0])
    final_book = replay_fixture(case['initial'], case['events'])
    terminal = final_book.snapshot(case['marks'])
    initial = initial_book.snapshot({instrument_id: '10'})
    expected = original['expected']
    for field in ('nav', 'cash_settled', 'realized_gross', 'fees', 'cost_basis', 'pnl_from_cost_origin'):
        assert Decimal(terminal[field]) == Decimal(expected[field]), (field, terminal[field], expected[field])
    twr = true_time_weighted_return([(initial['nav'], terminal['nav'])])
    assert twr == Decimal('0.009')

    artifacts = {}
    def artifact(key, payload, contract_name=None):
        artifacts[key] = deepcopy(payload)
        if contract_name:
            validate(contract_name, payload)
        return {
            'ref': ARTIFACT_FILE + '#/' + key, 'content_hash': digest(payload),
            'contract_id': ('urn:pulse:contracts:v3#/$defs/' + contract_name
                            if contract_name else 'fixture.' + key),
            'schema_version': '3.0.0' if contract_name else '1.0.0'}

    as_of = '2026-11-10T01:59:02Z'
    published_at = '2026-11-10T01:59:04Z'
    generated_at = '2026-11-10T01:59:07Z'
    validation_started_at = '2026-11-12T02:01:00Z'
    finished_at = '2026-11-12T02:01:01Z'
    policy = 'fixture-observed-clock-v1'
    market_rules = 'fixture-ordinary-cny-no-full-exchange-rules-v1'
    memberships = {'taxonomy_node': taxonomy, 'instrument_ids': [instrument_id],
                   'available_at': '2026-11-10T01:58:00Z', 'synthetic': True}
    artifact('membership_fixture', memberships)
    membership_ref = {'snapshot_id': uid('membership'), 'dataset_id': 'fixture.membership',
                      'manifest_version': 1, 'content_hash': digest(memberships)}
    bar = {
        'instrument_id': instrument_id, 'trade_date': '2026-11-10', 'timeframe': '1m',
        'session_scope': 'fixture.continuous', 'bar_open_time': '2026-11-10T01:58:00Z',
        'bar_close_time': '2026-11-10T01:59:00Z', 'snapshot_id': uid('bars'), 'currency': 'CNY',
        'adjustment_mode': 'raw', 'open_price_raw': '10', 'high_price_raw': '10',
        'low_price_raw': '10', 'close_price_raw': '10', 'volume_shares': 100,
        'turnover_amount_cny': '1000', 'exchange_reference_price_raw': '10', 'is_final': True,
        'available_at': '2026-11-10T01:59:00.100000Z', 'quality_status': 'valid', 'field_quality': {}}
    bar_artifact = artifact('bar', bar, 'CanonicalBar')
    bars_ref = {'snapshot_id': uid('bars'), 'dataset_id': 'fixture.raw_bars',
                'manifest_version': 1, 'content_hash': digest([bar])}
    snapshots = [bars_ref, membership_ref]
    feature_key = {
        'feature_id': 'fixture.raw_bar_close', 'definition_version': '1',
        'implementation_digest': digest('fixture raw field selection; no fitted model'),
        'parameter_digest': digest({}), 'instrument_ids': [instrument_id],
        'snapshot_id': uid('bars'), 'decision_as_of': as_of, 'availability_policy_version': policy,
        'timeframe': '1m', 'session_policy_version': 'fixture.continuous-v1',
        'input_revision': '1', 'input_schema_version': '3.0.0', 'dependency_digest': digest(snapshots),
        'calendar_version': 'fixture-dates-only-v1', 'adjustment_mode': 'raw', 'adjustment_version': 'raw-v1',
        'universe_policy_version': 'fixture-single-instrument-v1', 'market_rules_version': market_rules,
        'numeric_policy_version': NUMERIC_POLICY_VERSION}
    feature = {'feature_key_hash': digest(feature_key), 'key': feature_key, 'result_state': 'READY',
               'artifact': bar_artifact, 'computed_at': '2026-11-10T01:59:01Z', 'quality_status': 'valid'}
    validate('FeatureResult', feature)
    operand = {
        'kind': 'feature', 'feature_id': feature_key['feature_id'], 'definition_version': '1',
        'instrument_id': instrument_id, 'timeframe': '1m', 'field_path': 'close_price_raw',
        'unit': 'CNY/share', 'resolution_policy_version': 'fixture-latest-completed-bar-v1',
        'initial_feature_key_hash': feature['feature_key_hash']}
    clock = {'kind': 'bar_close', 'timeframe': '1m', 'session_policy_version': 'fixture.continuous-v1'}
    def condition(operator, price):
        return {'node': 'compare', 'clock': clock, 'operator': operator, 'left': operand,
                'right': {'kind': 'constant', 'value': price, 'unit': 'CNY/share'}}

    preflight = {
        'synthetic': True, 'scope': 'Schema/reference identity and existing Ledger order-reservation probe only',
        'not_implemented': ['full PlanCompiler', 'ConditionRunner', 'exchange matching', 'production strategy admission'],
        'initial_account_version': 0, 'probe_reserved_cash_cny': text_decimal(probe.orders['b1'].reserved_cash),
        'primary_account_was_not_reserved': initial_book.sequence == 0,
        'validated_at': '2026-11-10T01:59:03Z'}
    artifacts['preflight'] = preflight
    plan = {
        **plan_ref, 'instrument_id': instrument_id, 'based_on_release_id': release_id,
        'account_id': account_id, 'expected_account_version': 0, 'state': 'actionable',
        'strategy_refs': [strategy_ref], 'model_run_id': None, 'evidence_revision_ids': [], 'position_side': 'long',
        'entry_conditions': condition('lte', '10'),
        'entry_price_zone': {'lower_price_raw': '10', 'upper_price_raw': '10', 'currency': 'CNY', 'adjustment_mode': 'raw'},
        'sizing': {'kind': 'fixed_shares', 'quantity_shares': 100}, 'exit_conditions': condition('gte', '11'),
        'invalidation': condition('lt', '9'),
        'holding_horizon': {'horizon_id': 'fixture_two_trading_days', 'unit': 'trading_days', 'value': 2},
        'trigger_policy': {'trigger_id': 'fixture-one-entry', 'fire_mode': 'on_transition_to_true',
                           'unknown_policy': 'do_not_fire', 'rearm': {'mode': 'never', 'cooldown_ms': 0},
                           'max_fires': 1, 'min_interval_ms': 0, 'require_final_bar': True,
                           'pending_order_policy': 'block_retrigger'},
        'valid_from': published_at, 'expires_at': '2026-11-12T07:00:00Z',
        'replace_policy': 'cancel_remaining_and_reconcile',
        'validation': {'status': 'passed', 'policy_version': 'fixture-reservation-probe-only-v1',
                       'evaluation_id': None, 'result_ref': ARTIFACT_FILE + '#/preflight',
                       'validated_at': preflight['validated_at']},
        'available_at': preflight['validated_at'], 'supersedes': None}
    plan_artifact = artifact('plan', plan, 'TradePlan')
    explanation = 'Synthetic B01 contract fixture; score is an illustrative ordering value, not alpha evidence.'
    artifacts['explanation'] = {'synthetic': True, 'text': explanation}
    entry = {'instrument_id': instrument_id, 'alpha_rank': 1, 'action_rank': 1,
             'relative_rank_score': '1', 'calibrated_probability': None, 'action_status': 'actionable',
             'explanation_ref': ARTIFACT_FILE + '#/explanation', 'plan_ref': plan_ref}
    ranking_common = {'release_id': release_id, 'horizon_id': 'fixture_two_trading_days',
                      'forecast_target': 'synthetic_accounting_conformance_only',
                      'ranking_policy_version': 'fixture-single-item-v1', 'entries': [entry]}
    market = {**ranking_common, 'ranking_id': uid('market-ranking'), 'ranking_scope': 'market',
              'scope_key': 'fixture.market', 'taxonomy_node': None, 'membership_snapshot_id': None}
    sector = {**ranking_common, 'ranking_id': uid('sector-ranking'), 'ranking_scope': 'sector',
              'scope_key': 'fixture.industry/synthetic_sector', 'taxonomy_node': taxonomy,
              'membership_snapshot_id': membership_ref['snapshot_id']}
    market_artifact = artifact('market_ranking', market, 'Ranking')
    sector_artifact = artifact('sector_ranking', sector, 'Ranking')
    overview = {
        'ranking_id': uid('sector-overview'), 'release_id': release_id, 'ranking_scope': 'sector_overview',
        'taxonomy_id': taxonomy['taxonomy_id'], 'taxonomy_version': 1,
        'horizon_id': 'fixture_two_trading_days', 'forecast_target': 'synthetic_accounting_conformance_only',
        'ranking_policy_version': 'fixture-single-item-v1',
        'entries': [{'taxonomy_node': taxonomy, 'rank_kind': 'sector_opportunity_rank', 'rank': 1, 'score': '1',
                     'member_count': 1, 'observed_member_count': 1,
                     'membership_snapshot_id': membership_ref['snapshot_id'],
                     'explanation_ref': ARTIFACT_FILE + '#/explanation', 'quality_status': 'valid'}]}
    overview_artifact = artifact('sector_overview', overview, 'SectorOverview')
    bar_window = {
        'instrument_id': instrument_id, 'snapshot_id': uid('bars'), 'timeframe': '1m',
        'session_policy_version': 'fixture.continuous-v1', 'adjustment_mode': 'raw', 'adjustment_version': None,
        'start': bar['bar_open_time'], 'end': bar['bar_close_time'], 'window_complete': True,
        'window_hash': digest([bar]), 'bars': [bar]}
    window_artifact = artifact('bar_window', bar_window, 'BarWindow')
    instrument = {
        'instrument_id': instrument_id, 'venue_code': 'XSHG', 'instrument_type': 'equity', 'currency': 'CNY',
        'exchange_timezone': 'Asia/Shanghai', 'display_name': '合成样例证券（不是上市公司）',
        'state_version': 1, 'source_symbols': [{'source_id': 'fixture.synthetic', 'source_symbol': old_symbol}],
        'trading_status': 'active', 'is_risk_warning': False, 'board_code': 'fixture.mainboard'}
    workspace = {
        'instrument': instrument, 'release_id': release_id, 'view_kind': 'account_holding',
        'scope_key': account_id, 'projection_version': 1, 'projection_hash': '0' * 64,
        'chart_window_refs': [window_artifact], 'features': [feature], 'plan_refs': [plan_ref],
        'evidence_revision_ids': [],
        'strategy_contributions': [{'instance_ref': strategy_ref, 'role': 'entry', 'contribution_score': '1',
                                    'explanation_ref': ARTIFACT_FILE + '#/explanation'}],
        'actionability_id': None, 'quote_overlay': None,
        'generated_at': '2026-11-10T01:59:03.500000Z'}

    # Only this frozen projection belongs to the immutable release. Response-time
    # quote, actionability and generated_at are overlays, not a rewritten artifact.
    workspace['projection_hash'] = digest({k: v for k, v in workspace.items() if k != 'projection_hash'})
    workspace_artifact = artifact('workspace_frozen', workspace, 'Workspace')

    # A separate quote contract is integrated below; never manufacture it from bar close.
    quote_objects = build_quote_fixture(schema, instrument_id)
    quote_snapshot, quote_ref = quote_objects['snapshot'], quote_objects['reference']
    quote_artifact = artifact('quote_snapshot', quote_snapshot, 'QuoteSnapshot')
    validate('CanonicalQuote', quote_snapshot['quote'])
    workspace = deepcopy(workspace)
    workspace['quote_overlay'] = quote_snapshot
    workspace['actionability_id'] = uid('actionability')
    workspace['generated_at'] = generated_at
    artifact('workspace_response_overlay', workspace, 'Workspace')
    actionability = {
        'actionability_id': uid('actionability'), 'release_id': release_id, 'ranking_id': market['ranking_id'],
        'account_id': account_id, 'account_version': 0, 'quote_snapshot_id': quote_ref['snapshot_id'],
        'actionability_policy_version': 'fixture-reservation-probe-only-v1',
        'checked_at': '2026-11-10T01:59:06Z', 'valid_until': '2026-11-10T02:00:30Z',
        'entries': [{'instrument_id': instrument_id, 'plan_ref': plan_ref, 'frozen_action_rank': 1,
                     'account_action_rank': 1, 'actionability_status': 'eligible', 'side': 'buy',
                     'reason_codes': ['SYNTHETIC_RESERVATION_PROBE_PASSED'], 'checked_quantity_shares': 100}]}
    artifact('actionability', actionability, 'ActionabilitySnapshot')

    component_inputs = {'market_ranking': market_artifact, 'sector_ranking': sector_artifact,
                        'sector_overview': overview_artifact, 'plan': plan_artifact,
                        'workspace': workspace_artifact, 'bar_window': window_artifact}
    release = {
        'release_id': release_id, 'release_state': 'READY', 'decision_as_of': as_of,
        'availability_policy_version': policy, 'universe_policy_version': 'fixture-single-instrument-v1',
        'market_rules_version': market_rules, 'required_components': list(component_inputs),
        'release_hash': '0' * 64, 'published_at': published_at,
        'components': [{'component_key': name, 'component_state': 'READY', 'artifact': ref,
                        'snapshot_id': None, 'input_version_hash': digest(snapshots),
                        'computed_at': '2026-11-10T01:59:03.600000Z',
                        'valid_until': '2026-11-12T07:00:00Z'} for name, ref in component_inputs.items()]}
    release['release_hash'] = digest({k: v for k, v in release.items() if k != 'release_hash'})
    artifact('release', release, 'AnalysisRelease')

    def account_projection(book, marks, valuation_id):
        s = book.snapshot(marks)
        row = {
            'account_id': account_id, 'account_version': s['sequence'], 'base_currency': 'CNY',
            'trading_date': s['trading_day'], 'ledger_policy_version': POLICY_VERSION,
            'numeric_policy_version': NUMERIC_POLICY_VERSION, 'valuation_policy_version': 'fixture-raw-mark-v1',
            'valuation_snapshot_id': valuation_id, 'reserved_cash_cny': text_decimal(sum(
                (o.reserved_cash for o in book.orders.values()), Decimal('0'))),
            'withdrawable_cash_cny': None, 'positions': []}
        mapping = {
            'cash_settled_cny': 'cash_settled', 'trade_receivable_cny': 'trade_receivable',
            'trade_payable_cny': 'trade_payable', 'dividend_receivable_cny': 'dividend_receivable',
            'cash_available_cny': 'cash_available', 'nav_cny': 'nav', 'realized_gross_cny': 'realized_gross',
            'dividend_income_cny': 'dividend_income', 'pnl_from_cost_origin_cny': 'pnl_from_cost_origin',
            'cost_basis_cny': 'cost_basis', 'unrealized_cny': 'unrealized',
            'transaction_cost_cny': 'fees', 'withholding_tax_cny': 'withholding_tax',
            'net_external_capital_cny': 'net_external_capital'}
        # DTO serialization canonicalizes signed zero; economic amounts are unchanged.
        row.update({target: text_decimal(s[source]) for target, source in mapping.items()})
        for symbol, h in s['positions'].items():
            mark = marks[symbol]
            row['positions'].append({
                'instrument_id': symbol, 'quantity_shares': h['quantity'], 'sellable_quantity_shares': h['sellable'],
                'reserved_sell_quantity_shares': h['reserved_sell'], 'cost_basis_cny': h['cost'],
                'mark_price_raw': mark, 'market_value_cny': text_decimal(Decimal(mark) * h['quantity']),
                'mark_quality_status': 'valid'})
        return row

    initial_account = account_projection(initial_book, {instrument_id: '10'}, uid('bars'))
    terminal_account = account_projection(final_book, case['marks'], uid('terminal-marks'))
    artifact('initial_account', initial_account, 'AccountProjection')
    terminal_artifact = artifact('terminal_account', terminal_account, 'AccountProjection')
    rule_ref = artifact('availability_rule', {
        'synthetic': True, 'time_basis': 'logical fixture timestamps, not observed market availability',
        'rule': 'No actual source time is rewritten. This synthetic run has a declared virtual clock.',
        'observed_mode_rule': 'Production usable_at is at least actual completion and dependency availability.'})
    terminal_ref = {'snapshot_id': uid('terminal-marks'), 'dataset_id': 'fixture.terminal_marks',
                    'manifest_version': 1, 'content_hash': digest(case['marks'])}
    context = {
        'evaluation_context_id': uid('evaluation-context'), 'context_hash': '0' * 64,
        'forecast_target': 'synthetic_accounting_conformance_only', 'horizon_id': 'fixture_two_trading_days',
        'decision_as_of': validation_started_at,
        'availability_policy': {'availability_policy_version': policy, 'mode': 'observed',
                                'available_at_semantics': 'actual_processing_completion', 'rule_ref': rule_ref,
                                'assumed_ingestion_delay_ms': None,
                                'assumption_note': 'All records and times are hypothetical fixture values; no live observation is asserted.'},
        'universe_policy_version': 'fixture-single-instrument-v1',
        'input_snapshot_refs': snapshots + [terminal_ref],
        'train_start': '2026-10-31T16:00:00Z', 'train_end': '2026-11-01T16:00:00Z',
        'validation_start': '2026-11-09T16:00:00Z', 'validation_end': '2026-11-12T16:00:00Z',
        'blind_start': None, 'blind_end': None, 'embargo_seconds': 0,
        'sealed_at': validation_started_at}
    context['context_hash'] = digest({k: v for k, v in context.items() if k != 'context_hash'})
    validate('EvaluationContext', context)
    def metric(name, value, unit):
        return {'metric_name': name, 'metric_definition_version': 'fixture-conformance-v1', 'slice_key': 'B01',
                'value': value, 'unit': unit, 'lower_bound': None, 'upper_bound': None, 'confidence_level': None,
                'sample_count': 1, 'quality_status': 'valid', 'missing_reason': None}
    evaluation = {
        'experiment_id': uid('evaluation'), 'instance_id': strategy_ref['instance_id'],
        'evaluation_context_id': context['evaluation_context_id'], 'context_hash': context['context_hash'],
        'hypothesis_id': uid('hypothesis'), 'hypothesis_version': 1, 'run_purpose': 'validation',
        'run_state': 'completed',
        'policy_versions': {'market_rules_version': market_rules,
                            'execution_policy_version': 'predeclared-golden-fills-no-matcher-v1',
                            'cost_policy_version': 'fixture-five-cny-per-order-v1', 'ledger_policy_version': POLICY_VERSION,
                            'performance_policy_version': 'true-twr-exact-flow-boundaries-v1',
                            'numeric_policy_version': NUMERIC_POLICY_VERSION},
        'started_at': validation_started_at, 'completed_at': finished_at,
        'result': terminal_artifact,
        'metrics': [metric('fixture_terminal_nav', terminal['nav'], 'CNY'),
                    metric('fixture_net_pnl', terminal['pnl_from_cost_origin'], 'CNY'),
                    metric('fixture_transaction_cost', terminal['fees'], 'CNY'),
                    metric('fixture_twr', text_decimal(twr), 'ratio')],
        'error_code': None, 'context': context}
    artifact('evaluation', evaluation, 'Evaluation')

    def meta(name, *, terminal=False, action=False, live=False):
        return {
            'request_id': uid('request:' + name), 'generated_at': finished_at if terminal else generated_at,
            'trace_id': digest('trace:' + name)[:32], 'contract_version': '3.0.0',
            'release_id': None if terminal else release_id, 'decision_as_of': None if terminal else as_of,
            'availability_policy_version': policy,
            'input_snapshot_refs': (snapshots + [terminal_ref] if terminal else snapshots + ([quote_ref] if live else [])),
            'quality_status': 'valid', 'freshness_state': 'current' if live else 'not_expected',
            'coverage': {'policy_version': 'fixture-single-item-v1', 'expected_count': 1, 'observed_count': 1,
                         'missing_count': 0, 'unverifiable_reasons': []},
            'warnings': [{'code': 'SYNTHETIC_FIXTURE', 'message': 'All records, prices and times are hypothetical test values. No live data or alpha validation.',
                          'affected_component': name, 'evidence_ref': ARTIFACT_FILE + '#/preflight'}],
            'next_cursor': None, 'actionability_id': uid('actionability') if action else None}

    responses = {}
    cases_by_operation = {
        'getRelease': release, 'getMarketRanking': market, 'getSectorOverview': overview,
        'getSectorRanking': sector, 'getTradePlan': plan, 'getStockWorkspace': workspace,
        'getStockBars': bar_window, 'getAccountProjection': terminal_account,
        'getAccountActionability': actionability, 'getEvaluation': evaluation, 'getStockQuote': quote_snapshot}
    for operation, payload in cases_by_operation.items():
        envelope = {'meta': meta(operation, terminal=operation in ('getAccountProjection', 'getEvaluation'),
                                  action=operation in ('getAccountActionability', 'getStockWorkspace'),
                                  live=operation in ('getAccountActionability', 'getStockWorkspace', 'getStockQuote')),
                    'data': payload}
        validate(operation + 'Response', envelope)
        responses[operation] = envelope

    # Cross-document semantic checks, independent of JSON Schema's shape validation.
    assert all(r['release_id'] == release_id for r in (market, sector, overview, workspace, actionability))
    assert plan['based_on_release_id'] == release_id
    assert all(r['entries'][0]['instrument_id'] == instrument_id for r in (market, sector))
    assert all(r['entries'][0]['plan_ref'] == plan_ref for r in (market, sector))
    assert sector['taxonomy_node'] == overview['entries'][0]['taxonomy_node']
    assert sector['membership_snapshot_id'] == overview['entries'][0]['membership_snapshot_id']
    assert actionability['ranking_id'] == market['ranking_id'] and actionability['account_version'] == 0
    assert initial_account['positions'] == [] and actionability['entries'][0]['side'] == 'buy'
    assert plan['sizing']['quantity_shares'] == case['events'][0]['payload']['quantity']
    assert plan['entry_price_zone']['upper_price_raw'] == case['events'][0]['payload']['limit_price']
    assert set(release['required_components']) == {x['component_key'] for x in release['components']}
    assert Decimal(terminal_account['nav_cny']) == (Decimal(terminal_account['net_external_capital_cny'])
        + Decimal(terminal_account['realized_gross_cny']) + Decimal(terminal_account['unrealized_cny'])
        + Decimal(terminal_account['dividend_income_cny']) - Decimal(terminal_account['transaction_cost_cny'])
        - Decimal(terminal_account['withholding_tax_cny']))
    for component in release['components']:
        ref = component['artifact']; key = ref['ref'].split('#/', 1)[1]
        assert ref['content_hash'] == digest(artifacts[key])
    assert datetime.fromisoformat(bar['available_at']) <= datetime.fromisoformat(as_of)
    assert datetime.fromisoformat(feature['computed_at']) <= datetime.fromisoformat(as_of)
    assert datetime.fromisoformat(plan['available_at']) <= datetime.fromisoformat(published_at)
    assert datetime.fromisoformat(published_at) < datetime.fromisoformat(case['events'][0]['effective_at'])
    assert artifacts['workspace_frozen']['quote_overlay'] is None
    assert artifacts['workspace_frozen']['actionability_id'] is None
    assert workspace['projection_hash'] == artifacts['workspace_frozen']['projection_hash']
    assert datetime.fromisoformat(published_at) < datetime.fromisoformat(quote_snapshot['quote']['available_at'])
    assert datetime.fromisoformat(quote_snapshot['quote']['available_at']) <= datetime.fromisoformat(actionability['checked_at'])
    assert datetime.fromisoformat(actionability['checked_at']) <= datetime.fromisoformat(workspace['generated_at'])
    assert quote_snapshot['quote']['last_price_raw'] != bar['close_price_raw']

    report = {
        'schema_version': '1.0.0', 'synthetic': True, 'executed_at': datetime.now(timezone.utc).isoformat(),
        'kind': 'v3_contract_and_ledger_vertical_conformance',
        'production_website': False, 'real_market_data': False, 'strategy_alpha_test': False,
        'implementation_boundaries': preflight['not_implemented'] + ['PostgreSQL transactions', 'source/broker adapters', 'model training'],
        'clock_mode': 'hypothetical_fixture_clock_not_observed_market_time',
        'schema_sha256': hashlib.sha256(schema_bytes).hexdigest(),
        'ledger_sha256': hashlib.sha256((HERE / 'unified_ledger.py').read_bytes()).hexdigest(),
        'golden_fixture_sha256': hashlib.sha256((HERE / 'golden_fixtures.json').read_bytes()).hexdigest(),
        'golden_case_id': 'B01', 'identity_mapping': {old_symbol: instrument_id},
        'registry_fixture': {'strategy_instance': strategy_ref, 'hypothesis_id': uid('hypothesis'),
                             'note': 'Synthetic identities only; no production strategy registration or promotion performed.'},
        'schema_validations': len(validated), 'validated_contracts': validated,
        'api_response_fixtures': len(responses), 'golden_scenarios': 1,
        'unittest_methods_added': 0,
        'frozen_projection_and_live_overlays_separated': True,
        'cross_links_validated': True, 'known_golden_amounts_match': True,
        'economic_result': {'terminal_nav_cny': terminal['nav'], 'net_pnl_cny': terminal['pnl_from_cost_origin'],
                            'transaction_cost_cny': terminal['fees'], 'twr': text_decimal(twr)},
        'journal_sha256': final_book.journal_digest(),
        'evaluation_lineage': {'experiment_id': evaluation['experiment_id'], 'initial_release_id': release_id,
                               'executed_plan_ref': plan_ref, 'account_id': account_id,
                               'initial_account_version': 0, 'terminal_account_version': final_book.sequence,
                               'event_origin': 'Existing hand-calculated B01 events; not generated by a strategy/matcher'},
        'normalized_reference_events': case['events'], 'responses': responses,
        'notes': ['Evaluation context training interval is a structural placeholder; no model was fitted.',
                  'Evaluation is sealed after the synthetic scenario as accounting conformance, not a precommitted alpha experiment.',
                  'Plan validation policy is the named synthetic reservation probe only, not production approval.',
                  'The last bar and the independently supplied quote remain different typed objects.',
                  'Workspace projection_hash names the frozen artifact; live overlays and response generated_at do not mutate it.',
                  'No new matching or accounting formulas replace the existing Ledger.']}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / ARTIFACT_FILE).write_text(json.dumps(artifacts, ensure_ascii=False, indent=2) + '\n')
    (args.out_dir / RESULT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'PASS synthetic vertical slice: {len(validated)} v3 schema validations; one release; one ledger engine')
    print(f'B01 terminal NAV={terminal["nav"]} CNY; net PnL={terminal["pnl_from_cost_origin"]}; TWR={twr}')
    print('No production website, actual market execution, or strategy profitability is asserted.')


def build_quote_fixture(schema, instrument_id):
    """Explicitly supplied quote fixture, independently timed and priced from bars."""
    assert 'CanonicalQuote' in schema['$defs'] and 'QuoteSnapshot' in schema['$defs']
    snapshot = {
        'quote_snapshot_id': uid('live-quote'), 'quote_policy_version': 'fixture-independent-l1-v1',
        'quote': {
            'instrument_id': instrument_id, 'source_id': 'fixture.synthetic',
            'endpoint_id': 'fixture_l1', 'source_schema_version': '1', 'mapping_version': '1',
            'observation_id': uid('live-quote-observation'), 'live_quote_version': 1,
            'trade_date': '2026-11-10', 'source_event_time': '2026-11-10T01:59:05Z',
            'first_seen_at': '2026-11-10T01:59:05.050000Z',
            'available_at': '2026-11-10T01:59:05.100000Z',
            'currency': 'CNY', 'adjustment_mode': 'raw',
            'last_price_raw': '9.99', 'bid_price_raw': '9.99', 'ask_price_raw': '10',
            'bid_volume_shares': 200, 'ask_volume_shares': 100,
            'quality_status': 'valid', 'field_quality': {}},
        'freshness_state': 'current', 'freshness_evaluated_at': '2026-11-10T01:59:06Z'}
    reference = {
        'snapshot_id': snapshot['quote_snapshot_id'], 'dataset_id': 'fixture.live_quote',
        'manifest_version': 1, 'content_hash': digest(snapshot['quote'])}
    return {'snapshot': snapshot, 'reference': reference}


if __name__ == '__main__':
    main()

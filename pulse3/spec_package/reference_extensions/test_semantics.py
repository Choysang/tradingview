"""Boundary examples that structure-only validation would miss."""
import copy
from decimal import localcontext, Inexact
import unittest
from semantic_validation import *

I='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'
C='33333333-3333-4333-8333-333333333333'
S='44444444-4444-4444-8444-444444444444'
H='a'*64
T0='2026-11-10T01:00:00Z';T1='2026-11-10T01:30:00Z';T2='2026-11-10T07:00:00Z';T3='2026-11-10T07:01:00Z'

def forecast():
    return dict(instrument_id=I,target_spec_ref=dict(ref='fixture://target/binary-total-return',content_hash=H,contract_id='fixture.binary_target',schema_version='1.0'),
      forecast_as_of=T0,horizon_start_at=T1,horizon_end_at=T2,session_id='synthetic-session',prediction_probability='0.8',probability_origin='source_explicit',source_document_revision_id=S,source_evidence_ordinal=1,invalidation_condition='explicit target policy')
def outcome(f):
    return dict(claim_revision_id=C,forecast_payload_hash=canonical_hash(f),target_spec_hash=H,evaluation_snapshot_id=S,scoring_policy_version='brier_binary_decimal12_v1',horizon_end_at=T2,computed_at=T3,label_state='resolved',label_available_at=T2,label=1,brier_error='0.04',reason=None)
def layer():
    return dict(layer_id='fixture-volume-profile',layer_kind='volume_profile',required_capabilities=['minute_bars'],input_artifacts=[dict(ref='fixture://bars',content_hash=H,contract_id='fixture.bars',schema_version='1.0')],data_status='available',derivation_kind='estimated',price_basis='raw',window_start_at=T1,window_end_at=T2,available_at=T3,assumptions=['uniform bar-price allocation for this fixture only'],reason=None)
def macd():
    return dict(profile_version='fixture-1',timeframe='1m',fast_period=12,slow_period=26,signal_period=9,ema_seed='sma_seed',hist_multiplier='2',min_completed_bars=100,closed_bars_only=True,pivot_left_bars=3,pivot_right_bars=3,pivot_available_rule='right_closed_confirmation_only')

class SemanticTests(unittest.TestCase):
    def test_explicit_forecast_and_handcomputed_score(self):
        f=forecast();validate_forecast(f);validate_outcome(outcome(f),f,claim_revision_id=C)
    def test_no_probability_can_resolve_but_cannot_get_brier(self):
        f=forecast();f.update(prediction_probability=None,probability_origin='not_provided');o=outcome(f);o['brier_error']=None
        validate_outcome(o,f,claim_revision_id=C);o['brier_error']='0'
        with self.assertRaises(ValueError):validate_outcome(o,f,claim_revision_id=C)
    def test_bullish_stance_is_not_an_explicit_probability(self):
        f=forecast();f['probability_origin']='not_provided'
        check_schema('CommunityForecastPayload',f)
        with self.assertRaises(ValueError):validate_forecast(f)
    def test_probability_bounds_and_horizon(self):
        for field,value in [('prediction_probability','1.01'),('horizon_end_at',T0),('forecast_as_of',T2)]:
            with self.subTest(field=field):
                f=forecast();f[field]=value
                with self.assertRaises(ValueError):validate_forecast(f)
    def test_edited_forecast_cannot_reuse_outcome(self):
        f=forecast();o=outcome(f);f['prediction_probability']='0.9'
        with self.assertRaisesRegex(ValueError,'canonical payload'):validate_outcome(o,f,claim_revision_id=C)
    def test_different_claim_target_or_horizon_rejected(self):
        for field,value in [('claim_revision_id',B),('target_spec_hash','b'*64),('horizon_end_at',T1)]:
            with self.subTest(field=field):
                f=forecast();o=outcome(f);o[field]=value
                with self.assertRaises(ValueError):validate_outcome(o,f,claim_revision_id=C)
    def test_label_maturity_and_availability(self):
        for time in [T1,'2026-11-10T08:00:00Z']:
            f=forecast();o=outcome(f);o['label_available_at']=time
            with self.assertRaises(ValueError):validate_outcome(o,f,claim_revision_id=C)
    def test_pending_never_contains_future_score(self):
        f=forecast();o=outcome(f);o['label_state']='pending';o['reason']='waiting for maturity'
        with self.assertRaises(ValueError):validate_outcome(o,f,claim_revision_id=C)
        o.update(label=None,label_available_at=None,brier_error=None);validate_outcome(o,f,claim_revision_id=C)
    def test_incorrect_score_rejected_independent_of_decimal_context(self):
        f=forecast();f['prediction_probability']='0.123456789012';o=outcome(f)
        with localcontext() as context:
            context.prec=2;context.traps[Inexact]=True
            o['brier_error']='0.768328000729';validate_outcome(o,f,claim_revision_id=C)
            o['brier_error']='0.7'
            with self.assertRaises(ValueError):validate_outcome(o,f,claim_revision_id=C)
    def test_convertible_relation_needs_distinct_typed_entities(self):
        r=dict(underlying_instrument_id=I,effective_from=T0,effective_to=None)
        validate_relation(r,subject_id=B,known_instrument_types={B:'convertible_bond',I:'equity'})
        for subject,types in [(I,{I:'equity'}),(B,{B:'equity',I:'equity'}),(B,{B:'convertible_bond'})]:
            with self.assertRaises(ValueError):validate_relation(r,subject_id=subject,known_instrument_types=types)
    def test_relation_effective_interval(self):
        r=dict(underlying_instrument_id=I,effective_from=T1,effective_to=T0)
        with self.assertRaises(ValueError):validate_relation(r,subject_id=B,known_instrument_types={B:'convertible_bond',I:'equity'})
    def test_estimated_volume_layer_with_real_input_context(self):
        validate_layer(layer(),verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})
    def test_estimate_requires_assumptions(self):
        r=layer();r['assumptions']=[]
        with self.assertRaises(ValueError):validate_layer(r,verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})
    def test_snapshot_cannot_claim_complete_cancellation_rate(self):
        r=layer();r['layer_kind']='order_cancellation';r['required_capabilities']=['quote_snapshots']
        with self.assertRaises(ValueError):validate_layer(r,verified_capabilities={'quote_snapshots'},input_available_at={('fixture://bars',H):T2})
    def test_missing_capability_and_future_input_rejected(self):
        with self.assertRaises(ValueError):validate_layer(layer(),verified_capabilities=set(),input_available_at={('fixture://bars',H):T2})
        with self.assertRaises(ValueError):validate_layer(layer(),verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):'2026-11-10T08:00:00Z'})
    def test_macd_profile_declares_warmup_and_pivot_policy(self):
        validate_macd(macd())
        for field,value in [('fast_period',26),('min_completed_bars',20)]:
            r=macd();r[field]=value
            with self.assertRaises(ValueError):validate_macd(r)
    def test_cost_estimate_and_bar_allocation_cannot_claim_observed(self):
        for kind in ['cost_distribution_estimate','volume_profile']:
            r=layer();r.update(layer_kind=kind,derivation_kind='observed',assumptions=[])
            with self.assertRaises(ValueError):validate_layer(r,verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})
    def test_community_layer_cannot_be_built_from_bars(self):
        r=layer();r['layer_kind']='community'
        with self.assertRaises(ValueError):validate_layer(r,verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})
    def test_source_document_is_required_for_evidence_identity(self):
        f=forecast();del f['source_document_revision_id']
        with self.assertRaises(Exception):check_schema('CommunityForecastPayload',f)
    def test_equal_instant_horizon_is_not_string_equality(self):
        f=forecast();o=outcome(f);o['horizon_end_at']='2026-11-10T07:00:00.000Z'
        validate_outcome(o,f,claim_revision_id=C)
    def test_unimplemented_scoring_policy_is_not_silently_replaced(self):
        f=forecast();o=outcome(f);o['scoring_policy_version']='unregistered'
        with self.assertRaises(Exception):validate_outcome(o,f,claim_revision_id=C)
    def test_price_layer_needs_price_basis(self):
        r=layer();r['price_basis']='not_applicable'
        with self.assertRaises(ValueError):validate_layer(r,verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})

    def test_price_basis_reuses_base_adjustment_modes(self):
        for basis in ['raw','forward_adjusted','backward_adjusted']:
            r=layer();r['price_basis']=basis
            validate_layer(r,verified_capabilities={'minute_bars'},input_available_at={('fixture://bars',H):T2})
        for unregistered in ['qfq','hfq']:
            r=layer();r['price_basis']=unregistered
            with self.assertRaises(Exception):check_schema('ChartLayerDescriptor',r)

    def test_claim_token_uniquely_resolves_registry_identity(self):
        for token,identity in [('pulse.community_forecast@1.0',('pulse.community_forecast','1.0')),('pulse.convertible_relation@1.0',('pulse.convertible_relation','1.0'))]:
            binding=resolve_claim_binding(token)
            self.assertEqual((binding['contract_id'],binding['schema_version']),identity)
        for token in ['1.0','pulse.community_forecast@latest','pulse.community_forecast@2.0']:
            with self.assertRaises(ValueError):resolve_claim_binding(token)

    def test_bound_claim_checks_subject_predicate_and_assertion(self):
        context=dict(claim_schema_version='pulse.community_forecast@1.0',subject_kind='instrument',subject_key=I,predicate='community_binary_forecast',assertion_type='forecast')
        self.assertEqual(validate_bound_claim(forecast(),**context),('pulse.community_forecast','1.0'))
        for key,value in [('subject_kind','sector'),('subject_key',B),('predicate','convertible_underlying_of'),('assertion_type','opinion')]:
            wrong=dict(context);wrong[key]=value
            with self.assertRaises(ValueError):validate_bound_claim(forecast(),**wrong)
        relation=dict(underlying_instrument_id=I,effective_from=T0,effective_to=None)
        self.assertEqual(validate_bound_claim(relation,claim_schema_version='pulse.convertible_relation@1.0',subject_kind='instrument',subject_key=B,predicate='convertible_underlying_of',assertion_type='official_disclosure',known_instrument_types={B:'convertible_bond',I:'equity'}),('pulse.convertible_relation','1.0'))
        with self.assertRaises(Exception):validate_bound_claim(relation,**context)

    def test_late_arrival_does_not_rewrite_original_forecast_or_backdate_visibility(self):
        f=forecast();f.update(forecast_as_of=T1,horizon_start_at=T1)
        original_hash=canonical_hash(f)
        context=dict(source_published_at=T1,document_available_at='2026-11-10T01:30:03Z',claim_available_at='2026-11-10T01:30:04Z')
        self.assertFalse(forecast_known_at(f,decision_as_of='2026-11-10T01:30:01Z',**context))
        self.assertTrue(forecast_known_at(f,decision_as_of='2026-11-10T01:30:05Z',**context))
        self.assertEqual(canonical_hash(f),original_hash)
        reordered=dict(reversed(list(f.items())))
        self.assertEqual(canonical_payload_bytes(reordered),canonical_payload_bytes(f))
        self.assertEqual(canonical_hash(reordered),original_hash)
        wrong=dict(context);wrong['claim_available_at']=T1
        with self.assertRaises(ValueError):forecast_known_at(f,decision_as_of=T3,**wrong)

if __name__=='__main__':unittest.main()

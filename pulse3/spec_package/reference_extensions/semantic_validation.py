"""Bounded semantic checks on five strict payloads; no DB or market connectivity.

Repository checks listed in extension_bindings.json remain necessary. This module
does not prove a cited document or a capability is authentic; it compares the
explicit verified context passed by its caller.
"""
from datetime import datetime
from decimal import Decimal, Context, ROUND_HALF_EVEN, localcontext, InvalidOperation, DivisionByZero, Overflow
from hashlib import sha256
from pathlib import Path
import json
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT=Path(__file__).resolve().parents[1]
BASE=json.loads((ROOT/'contracts/v3.schema.json').read_text())
EXTRA=json.loads((ROOT/'contracts/supplemental.schema.json').read_text())
BINDINGS=json.loads((ROOT/'contracts/extension_bindings.json').read_text())
REGISTRY=Registry().with_resources([(BASE['$id'],Resource.from_contents(BASE)),(EXTRA['$id'],Resource.from_contents(EXTRA))])
QUANTUM=Decimal('0.000000000001')

def score_context():
    return Context(prec=50,rounding=ROUND_HALF_EVEN,Emin=-999999,Emax=999999,capitals=1,clamp=0,
                   flags=[],traps=[InvalidOperation,DivisionByZero,Overflow])

def canonical_payload_bytes(value):
    """Project UTF-8 JSON payload policy, not original document bytes or RFC8785."""
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def canonical_hash(value):
    return sha256(canonical_payload_bytes(value)).hexdigest()

def instant(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('timezone required')
    return result

def check_schema(name,value):
    Draft202012Validator({'$ref':EXTRA['$id']+'#/$defs/'+name},registry=REGISTRY,format_checker=FormatChecker()).validate(value)

def resolve_claim_binding(claim_schema_version):
    """Exact qualified token lookup; no unqualified version or latest fallback."""
    matches=[b for b in BINDINGS['bindings'] if b.get('wire_schema_token')==claim_schema_version]
    if len(matches)!=1:raise ValueError('unknown or ambiguous qualified claim schema token')
    binding=matches[0]
    if claim_schema_version!=binding['contract_id']+'@'+binding['schema_version']:
        raise ValueError('claim schema token differs from its registry identity')
    return dict(binding)

def validate_bound_claim(value,*,claim_schema_version,subject_kind,subject_key,predicate,assertion_type,known_instrument_types=None):
    """Validate the new payload's binding, not the entire EvidenceClaim Repository."""
    binding=resolve_claim_binding(claim_schema_version)
    if subject_kind!=binding['required_subject_kind']:raise ValueError('wrong claim subject kind')
    if predicate!=binding['required_predicate']:raise ValueError('wrong claim predicate')
    if assertion_type not in binding['allowed_assertion_types']:raise ValueError('wrong claim assertion type')
    if binding['definition']=='CommunityForecastPayload':
        validate_forecast(value)
        if subject_key!=value['instrument_id']:raise ValueError('forecast subject differs from payload instrument')
    elif binding['definition']=='ConvertibleRelationPayload':
        if known_instrument_types is None:raise ValueError('relation requires verified instrument-type context')
        validate_relation(value,subject_id=subject_key,known_instrument_types=known_instrument_types)
    else:
        raise ValueError('claim binding implementation is not registered')
    return binding['contract_id'],binding['schema_version']

def validate_forecast(value):
    check_schema('CommunityForecastPayload',value)
    if not instant(value['forecast_as_of']) <= instant(value['horizon_start_at']) < instant(value['horizon_end_at']):
        raise ValueError('forecast must precede its positive-duration horizon')
    prob=value['prediction_probability']
    if value['probability_origin']=='not_provided':
        if prob is not None:raise ValueError('cannot infer a source probability')
    elif prob is None or not Decimal('0') <= Decimal(prob) <= Decimal('1'):
        raise ValueError('explicit probability must lie in [0,1]')

def forecast_known_at(value,*,source_published_at,document_available_at,claim_available_at,decision_as_of):
    """Knowledge visibility from trusted timestamps; not active-plan eligibility.

    Repository still verifies source publication/version evidence and assigns
    forward-collected versus retrospective evaluation cohorts. This function
    never rewrites a source prediction or backdates platform availability.
    """
    validate_forecast(value)
    source=instant(source_published_at);original=instant(value['forecast_as_of'])
    document=instant(document_available_at);claim=instant(claim_available_at)
    if source>original:raise ValueError('source version does not support original prediction cutoff')
    if document<source or claim<document:raise ValueError('actual platform availability is inconsistent')
    return max(original,document,claim)<=instant(decision_as_of)

def brier_error(probability,label):
    # Independent context prevents another module's precision/traps changing a score.
    with localcontext(score_context()):
        return (Decimal(probability)-Decimal(label))**2

def validate_outcome(value,forecast,*,claim_revision_id):
    check_schema('CommunityOutcomePayload',value);validate_forecast(forecast)
    if value['claim_revision_id']!=claim_revision_id:raise ValueError('wrong source claim revision')
    if value['forecast_payload_hash']!=canonical_hash(forecast):raise ValueError('forecast canonical payload changed')
    if value['target_spec_hash']!=forecast['target_spec_ref']['content_hash']:raise ValueError('wrong target definition')
    if instant(value['horizon_end_at'])!=instant(forecast['horizon_end_at']):raise ValueError('wrong horizon')
    computed=instant(value['computed_at']);end=instant(value['horizon_end_at'])
    if computed<instant(forecast['forecast_as_of']):raise ValueError('outcome predates forecast')
    if value['label_state']=='resolved':
        if value['label'] is None or value['label_available_at'] is None:raise ValueError('resolved needs an observed label')
        if not end<=instant(value['label_available_at'])<=computed:raise ValueError('label unavailable or premature')
        if value['reason'] is not None:raise ValueError('resolved outcome has no missing reason')
        p=forecast['prediction_probability']
        if p is None:
            if value['brier_error'] is not None:raise ValueError('unquoted probability is not scored')
        else:
            with localcontext(score_context()):
                expected=brier_error(p,value['label']).quantize(QUANTUM)
            if value['brier_error'] is None or Decimal(value['brier_error'])!=expected:raise ValueError('incorrect Brier score')
    else:
        if any(value[k] is not None for k in ['label','label_available_at','brier_error']):raise ValueError('unresolved output has no future label')
        if value['reason'] is None:raise ValueError('unresolved needs reason')

def validate_relation(value,*,subject_id,known_instrument_types):
    check_schema('ConvertibleRelationPayload',value)
    if value['effective_to'] is not None and instant(value['effective_to'])<=instant(value['effective_from']):raise ValueError('invalid effective interval')
    if subject_id==value['underlying_instrument_id']:raise ValueError('self relation')
    if known_instrument_types.get(subject_id)!='convertible_bond':raise ValueError('subject is not a known convertible')
    if known_instrument_types.get(value['underlying_instrument_id'])!='equity':raise ValueError('underlying is not a known equity')

def validate_layer(value,*,verified_capabilities,input_available_at):
    check_schema('ChartLayerDescriptor',value)
    if instant(value['window_start_at'])>=instant(value['window_end_at']):raise ValueError('invalid layer window')
    if value['derivation_kind']=='estimated' and not value['assumptions']:raise ValueError('estimate requires model assumptions')
    needs=set(value['required_capabilities'])
    kind=value['layer_kind']
    if kind in {'anchored_vwap','volume_profile','cost_distribution_estimate'} and value['price_basis']=='not_applicable':
        raise ValueError('price layer requires an explicit price basis')
    if kind in {'anchored_vwap','volume_profile','cost_distribution_estimate'} and not needs & {'minute_bars','individual_trades'}:
        raise ValueError('price distribution layer requires price-volume observations')
    if kind=='cost_distribution_estimate' and value['derivation_kind']!='estimated':
        raise ValueError('cost distribution remains an estimate')
    if kind=='volume_profile' and 'individual_trades' not in needs and value['derivation_kind']!='estimated':
        raise ValueError('bar allocation is an estimated volume profile')
    if kind=='auction_imbalance' and not needs & {'quote_snapshots','full_order_events'}:
        raise ValueError('auction imbalance needs auction observations')
    if kind in {'news','seat'} and 'historical_documents' not in needs:
        raise ValueError('event layer needs versioned evidence')
    if kind=='community' and 'historical_community' not in needs:
        raise ValueError('community layer needs observed historical samples')
    if kind=='plan' and 'published_plans' not in needs:
        raise ValueError('plan layer needs a published plan version')
    if value['layer_kind']=='order_cancellation' and 'full_order_events' not in needs:raise ValueError('cancellation requires full order sequence')
    if value['data_status']=='available':
        if not needs<=set(verified_capabilities):raise ValueError('capability unavailable')
        if not value['input_artifacts'] or value['reason'] is not None:raise ValueError('available layer needs inputs and no missing reason')
    elif value['reason'] is None:raise ValueError('partial/unavailable needs reason')
    for artifact in value['input_artifacts']:
        key=(artifact['ref'],artifact['content_hash'])
        if key not in input_available_at:raise ValueError('input manifest unresolved')
        if instant(value['available_at'])<instant(input_available_at[key]):raise ValueError('layer predates input')

def validate_macd(value):
    check_schema('MacdProfile',value)
    if value['fast_period']>=value['slow_period']:raise ValueError('fast must be shorter than slow')
    if value['min_completed_bars']<value['slow_period']+value['signal_period']-1:raise ValueError('insufficient registered warmup')
    # Actual pivot time and numerical indicator computation are separate tasks.


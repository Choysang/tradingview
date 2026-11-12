"""Independent boundary probes for supplemental payload semantics.

This is not a live source, database, capability authenticity or profitability test.
Each record states whether the reference validator accepts the constructed case.
"""
from pathlib import Path
import importlib.util, json, hashlib, copy
PKG=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('reviewed_semantics',PKG/'reference_extensions/semantic_validation.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
I='11111111-1111-4111-8111-111111111111';C='33333333-3333-4333-8333-333333333333';S='44444444-4444-4444-8444-444444444444';H='a'*64
T0='2026-11-10T01:00:00Z';T1='2026-11-10T01:30:00Z';T2='2026-11-10T07:00:00Z';T3='2026-11-10T07:01:00Z'
def layer():
 return dict(layer_id='review-volume',layer_kind='volume_profile',required_capabilities=['minute_bars'],input_artifacts=[dict(ref='fixture://minute-bars',content_hash=H,contract_id='fixture.minute_bars',schema_version='1.0')],data_status='available',derivation_kind='estimated',price_basis='raw',window_start_at=T1,window_end_at=T2,available_at=T3,assumptions=['uniform within bar; model estimate'],reason=None)
def forecast():
 return dict(instrument_id=I,target_spec_ref=dict(ref='fixture://binary-target',content_hash=H,contract_id='fixture.binary_target',schema_version='1.0'),forecast_as_of=T0,horizon_start_at=T1,horizon_end_at=T2,session_id='review-session',prediction_probability='0.8',probability_origin='source_explicit',source_document_revision_id=I,source_evidence_ordinal=1,invalidation_condition='target explicit invalidation')
def outcome(f):
 return dict(claim_revision_id=C,forecast_payload_hash=m.canonical_hash(f),target_spec_hash=H,evaluation_snapshot_id=S,scoring_policy_version='brier_binary_decimal12_v1',horizon_end_at=T2,computed_at=T3,label_state='resolved',label_available_at=T2,label=1,brier_error='0.04',reason=None)
results=[]
def probe(case_id,kind,description,fn):
 try:fn();state='accepted';error=None
 except Exception as e:state='rejected';error=type(e).__name__+': '+str(e).splitlines()[0]
 results.append(dict(case_id=case_id,kind=kind,description=description,validator_result=state,error=error))
ctx={'verified_capabilities':{'minute_bars'},'input_available_at':{('fixture://minute-bars',H):T2}}
probe('X00','control','Baseline estimated minute-bar volume profile',lambda:m.validate_layer(layer(),**ctx))
a=layer();a.update(layer_kind='cost_distribution_estimate',derivation_kind='observed',assumptions=[])
probe('X01','local_semantic_contradiction','Cost-distribution estimate relabelled observed with no assumptions',lambda:m.validate_layer(a,**ctx))
b=layer();b.update(layer_kind='community',derivation_kind='observed',assumptions=[],price_basis='not_applicable')
probe('X02','local_capability_contract_gap','Community layer satisfied by minute bars alone',lambda:m.validate_layer(b,**ctx))
c=layer();c.update(layer_kind='anchored_vwap',derivation_kind='deterministic',assumptions=[],price_basis='not_applicable')
probe('X03','local_semantic_contradiction','AVWAP price layer with not_applicable price basis',lambda:m.validate_layer(c,**ctx))
d=layer();d.update(derivation_kind='deterministic',assumptions=[])
probe('X04','local_resolution_contract_gap','Minute-bar allocated volume profile classified as deterministic exact result',lambda:m.validate_layer(d,**ctx))
e=layer();e.update(layer_kind='order_cancellation',required_capabilities=['full_order_events'],derivation_kind='observed',assumptions=[])
probe('X05','repository_context_gap','Full-order capability exists somewhere in caller global set, but only minute-bar artifact bound',lambda:m.validate_layer(e,verified_capabilities={'minute_bars','full_order_events'},input_available_at=ctx['input_available_at']))
f=forecast();o=outcome(f);o['horizon_end_at']='2026-11-10T07:00:00.000Z'
probe('X06','equivalent_timestamp_false_rejection','Same horizon instant, allowed UTC Z spelling with zero fractional seconds',lambda:m.validate_outcome(o,f,claim_revision_id=C))
g=forecast();q=outcome(g);q['scoring_policy_version']='unregistered-policy-with-different-rounding'
probe('X07','scoring_policy_registry_gap','Arbitrary scoring version still executes fixed Brier quantization',lambda:m.validate_outcome(q,g,claim_revision_id=C))
h=layer();h['window_end_at']='2027-01-01T00:00:00Z'
probe('X08','repository_coverage_gap','Available volume profile claims future data window without a coverage context',lambda:m.validate_layer(h,**ctx))
i=layer();i['input_artifacts'][0]['contract_id']='unrelated.future_contract'
probe('X09','repository_artifact_type_gap','Known ref/hash with altered artifact contract identity still accepted',lambda:m.validate_layer(i,**ctx))
j=forecast();j.pop('source_document_revision_id')
probe('X11','fixed_identity_required','Missing document identity is rejected by strict forecast schema',lambda:m.validate_forecast(j))
k=forecast();v=outcome(k)
probe('X12','control','Baseline explicit forecast and hand-computed Brier with supported policy',lambda:m.validate_outcome(v,k,claim_revision_id=C))
# Deterministic ambiguity example from the actual v3 SQL primary key.
evidence=[{'claim_revision_id':C,'document_revision_id':I,'evidence_ordinal':1,'probability':'0.8'}, {'claim_revision_id':C,'document_revision_id':S,'evidence_ordinal':1,'probability':'0.2'}]
lookup=[x for x in evidence if x['claim_revision_id']==C and x['evidence_ordinal']==forecast()['source_evidence_ordinal']]
full_lookup=[x for x in lookup if x['document_revision_id']==forecast()['source_document_revision_id']]
assert len(lookup)==2 and len(full_lookup)==1
results.append(dict(case_id='X10',kind='identity_contract_gap',description='Ordinal-only source pointer matches two legal claim_evidence rows',old_ordinal_only_matching_rows=len(lookup),full_identity_matching_rows=len(full_lookup),validator_result='historical_issue_fixed_by_source_document_revision_id',evidence_primary_key=['claim_revision_id','document_revision_id','evidence_ordinal']))
expected={'X00':'accepted','X01':'rejected','X02':'rejected','X03':'rejected','X04':'rejected','X05':'accepted','X06':'accepted','X07':'rejected','X08':'accepted','X09':'accepted','X10':'historical_issue_fixed_by_source_document_revision_id','X11':'rejected','X12':'accepted'}
for result in results:
 result['matches_documented_boundary']=result['validator_result']==expected[result['case_id']]
 assert result['matches_documented_boundary'], result
files=['contracts/supplemental.schema.json','contracts/extension_bindings.json','reference_extensions/semantic_validation.py','reference_extensions/test_semantics.py']
report={'checked_at':'2026-11-11','reviewed_sha256':{p:hashlib.sha256((PKG/p).read_bytes()).hexdigest() for p in files},'cases':results,'implemented_boundary_checks':10,'known_repository_gap_demonstrations':3,'all_documented_boundaries_match':True,'scope':'Synthetic contract/semantic boundaries only; repository checks are not implemented and are not presumed proven.'}
(PKG/'verification/extension_independent_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))

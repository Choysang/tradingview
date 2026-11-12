"""Run the bounded reference verification suites with explicit logs and scope."""
from pathlib import Path
from datetime import datetime, timezone
import subprocess, sys, json, platform

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'verification'; OUT.mkdir(exist_ok=True)
commands=[
 ('ledger_unit','execution',[sys.executable,'-m','unittest','-v','test_unified_ledger.py'],'43 unittest cases; one normalized CNY account reducer'),
 ('ledger_golden','execution',[sys.executable,'run_benchmark.py'],'10 hand-computed cases, three transport harnesses, ONE ledger'),
 ('data_plane_unit','reference_system',[sys.executable,'-m','unittest','-v','test_data_plane.py'],'39 unittest cases; bounded mappings and SQLite transaction reference'),
 ('feature_unit','reference_features',[sys.executable,'-m','unittest','-v','test_feature_store.py'],'19 unittest cases; in-process shared feature work'),
 ('source_examples','sources_sectors',[sys.executable,'validate_fixtures.py'],'21 synthetic source/sector checks; no live provider'),
 ('catalog_consistency','catalogs',[sys.executable,'validate_catalogs.py'],'20 catalog consistency checks; candidates not promoted'),
 ('sql_contract_static','integration',[sys.executable,'validate_integration.py'],'SQL/OAS/JSON Schema/reference checks; no PostgreSQL runtime'),
 ('contract_independent_static','execution',[sys.executable,'review_contracts_static.py'],'independent FK columns/types/unique-target and contract boundary review; no SQL runtime'),
 ('synthetic_vertical_slice','execution',[sys.executable,'synthetic_vertical_slice.py'],'same-release fixtures through formal schemas and sole account reducer; no website/network'),
 ('alignment_reference','reference_alignment',[sys.executable,'run_validation.py'],'27 author unittest cases and 8 policy-schema fixtures; observed-mode synthetic reference'),
 ('supplement_payload_unit','reference_extensions',[sys.executable,'-m','unittest','-v','test_semantics.py'],'26 author unit cases; five strict payloads and bounded semantic helpers'),
 ('ranking_continuity_unit','reference_transition',[sys.executable,'-m','unittest','-v','test_continuity.py'],'13 author unit cases; exact ranking trial budgets and explicit baseline residual; no capital allocation'),
 ('alignment_independent','verification',[sys.executable,'independent_alignment_review.py'],'16 independent unittest cases; separate fixture authorship, same bounded reference'),
 ('payload_independent','verification',[sys.executable,'extension_independent_review.py'],'13 probes: 10 implemented boundary checks and 3 known unimplemented Repository gaps'),
 ('supplement_static','verification',[sys.executable,'validate_supplement_v31.py'],'supplement identity, schema, actual SQL and API bindings, source/decision/baseline checks; no live runtime'),
 ('review_v32_static','verification',[sys.executable,'validate_review_v32.py'],'eleven original hashes, 1524 units/590 decisions, 55 input references, appendix and product mappings, proposed type closure and unchanged baseline; static only'),
]
started=datetime.now(timezone.utc).isoformat(); results=[]
for name,cwd,cmd,scope in commands:
    result=subprocess.run(cmd,cwd=ROOT/cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
    log={'command':cmd,'cwd':cwd,'exit_code':result.returncode,'scope':scope}
    (OUT/(name+'.log')).write_text(json.dumps(log,ensure_ascii=False)+'\n\n'+result.stdout)
    results.append({'suite':name,**log,'log':'verification/'+name+'.log'})
    print(name+': '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
    if result.returncode:print(result.stdout[-7000:],flush=True)
sources=json.loads((ROOT/'research/source_index.json').read_text())
report={'started_at':started,'finished_at':datetime.now(timezone.utc).isoformat(),'python_version':platform.python_version(),
 'all_executed_suites_passed':all(r['exit_code']==0 for r in results),'suites':results,
 'author_unit_test_cases':167,'independent_alignment_unit_test_cases':16,'unit_test_cases':183,'ranking_continuity_unit_test_cases':13,'alignment_policy_fixture_checks':8,'extension_probe_count':13,'extension_implemented_boundary_checks':10,'extension_known_repository_gaps':3,'golden_cases':10,'golden_transport_passes':30,'independent_ledger_engines':1,
 'source_records':len(sources),'unique_source_urls':len({s['url'] for s in sources}),
 'source_synthetic_checks':21,'catalog_checks':20,'real_postgres_executed':False,'live_data_connected':False,'production_load_tested':False,
 'website_implemented':False,'paper_training_or_returns_reproduced':False}
(OUT/'run_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if report['all_executed_suites_passed'] else 1)

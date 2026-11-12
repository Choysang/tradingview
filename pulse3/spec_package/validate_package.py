"""Validate delivery structure and explicit evidence boundaries, not production behavior."""
from pathlib import Path
from collections import Counter
import ast, json, re, hashlib, argparse, unittest, importlib.util

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--verify-hashes',action='store_true')
    args=parser.parse_args(); root=args.root.resolve(); checks=[]
    def check(name,condition):
        if not condition: raise AssertionError(name)
        checks.append(name)
    def load(rel): return json.loads((root/rel).read_text())
    active={str(p.relative_to(root)):json.loads(p.read_text()) for p in root.rglob('*.json') if 'archive' not in p.relative_to(root).parts and p.name!='SHA256SUMS.json'}
    check('active JSON documents parse',bool(active))
    authority=load('authority_manifest.json')
    check('all authority paths exist',all((root/p).is_file() for paths in authority['authorities'].values() for p in paths))
    check('one formal API contract directory',not any((root/'integration'/f).exists() for f in ['openapi.json','v3.schema.json','api_bindings.json']))
    check('v2 remains identified history',(root/'archive/v2/README.md').exists() and 'archive/v2' in authority['historical_only'])
    for name in ['source_catalog_v3.json','taxonomy.json']:
        check('catalog mirror '+name,(root/'catalogs'/name).read_bytes()==(root/'sources_sectors'/name).read_bytes())
    sources=load('research/source_index.json'); ids={x['id'] for x in sources}
    check('304 unique source record identities and 288 distinct URLs',len(sources)==len(ids)==304 and len({s['url'] for s in sources})==288)
    check('source URLs are explicit HTTP(S)',all(isinstance(x['url'],str) and x['url'].startswith(('http://','https://')) for x in sources))
    missing=[]
    for p in root.rglob('*.md'):
        if 'archive' in p.relative_to(root).parts: continue
        mentioned=set(re.findall(r'\[((?:A|T|X|DB|EXS|NS|CS|AP|EP|CR)\d{2}|(?:VM|SP)-S\d{2}[a-z]?|R32-[SP]\d{2})\]',p.read_text()))
        if mentioned-ids:missing.append((str(p.relative_to(root)),sorted(mentioned-ids)))
    check('source citations resolve: '+repr(missing),not missing)
    papers=load('research/papers_registry.json')['papers']; pids={x['paper_id'] for x in papers}
    check('46 distinct paper cards',len(papers)==len(pids)==46)
    check('reading depth counts precise',Counter(x['read_depth'] for x in papers)=={'targeted_fulltext':37,'abstract_only':7,'abstract_and_publisher_excerpt':2})
    check('no invented paper reproduction',all(x['reproduction_status']=='not_started' and x['implementation_status']=='not_implemented' for x in papers))
    check('paper publication status explicit',all(x.get('publication_status_code') and x.get('publication_status_note') for x in papers))
    hypotheses=load('research/hypotheses.json')['hypotheses']
    check('hypothesis mapping covers exact paper set',{x['paper_ref'] for x in hypotheses}==pids and all(not x['profitability_validated'] for x in hypotheses))
    platforms=load('research/platforms.json')
    check('10 platforms and 56 mapped capabilities',len(platforms['platforms'])==10 and len(platforms['records'])==56 and all(x['source_id'] in ids for x in platforms['records']))
    inv=load('traceability/code_inventory.json'); syms=load('traceability/symbol_inventory.json')
    check('old code inventory scope retained',inv['tracked_files']==len(inv['files'])==435 and len(syms['symbols'])==3484 and sum(inv['review_counts'].values())==435)
    reqs=load('traceability/requirements_map_v3.json')['requirements']; rids={r['requirement_id'] for r in reqs}
    check('38 requirements with valid paths',len(reqs)==len(rids)==38 and all((root/p).is_file() for r in reqs for p in r['specification_or_reference_paths']+r['verification_paths']))
    check('requirement source refs resolve',all(set(r['source_refs'])<=ids for r in reqs))
    reasons=load('traceability/reason_records_v3.json')['records']
    check('component reason records resolve',all((root/r['component_path']).is_file() and set(r['requirement_ids'])<=rids for r in reasons))
    bindings=load('contracts/api_bindings.json'); oas=load('contracts/openapi.json'); schemas=load('contracts/v3.schema.json')['$defs']
    opids={v['operationId'] for p in oas['paths'].values() for k,v in p.items() if k in {'get','post','put','delete','patch','options','head'}}
    check('31 operations have one-to-one bindings',len(opids)==31==len(bindings['operations']) and opids=={r['operation_id'] for r in bindings['operations']})
    check('migration order agrees',bindings['migration_order']==authority['migration_order'])
    check('quote distinct from bar',{'CanonicalQuote','QuoteSnapshot'}<=schemas.keys() and 'getStockQuote' in opids)
    check('only six strategy roles',set(schemas['StrategyRole']['enum'])=={'selection','allocation','entry','holding','execution','exit'})
    check('one plan state vocabulary',set(schemas['PlanState']['enum'])=={'watching','actionable','holding','superseded','expired'})
    check('shared response metadata',{'generated_at','trace_id','input_snapshot_refs','release_id','decision_as_of'}<=schemas['ResponseMeta']['properties'].keys() and 'code' in schemas['Error']['properties'])
    counts={}
    for folder,name in [('execution','test_unified_ledger.py'),('reference_system','test_data_plane.py'),('reference_features','test_feature_store.py'),('reference_alignment','test_alignment.py'),('reference_extensions','test_semantics.py'),('reference_transition','test_continuity.py')]:
        counts[folder]=unittest.TestLoader().discover(str(root/folder),pattern=name).countTestCases()
    check('167 author reference unittest cases including 13 ranking-continuity tests',counts=={'execution':43,'reference_system':39,'reference_features':19,'reference_alignment':27,'reference_extensions':26,'reference_transition':13})
    check('sources remain unconfigured',all(not s['enabled'] and s['integration_status']=='unconfigured' for s in load('catalogs/source_catalog_v3.json')['sources']))
    check('strategies remain candidates',all(not s['routing']['production_route_enabled'] and s['implementation_status']=='not_implemented' for s in load('catalogs/strategy_catalog_v3.json')['entries']))
    check('no full publisher PDFs redistributed',not any(p.suffix.lower()=='.pdf' for p in (root/'research').rglob('*')))
    review_module_spec=importlib.util.spec_from_file_location('_review_v32_validator',root/'verification/validate_review_v32.py')
    review_module=importlib.util.module_from_spec(review_module_spec)
    review_module_spec.loader.exec_module(review_module)
    review_result=review_module.validate(root)
    check('v3.2 review cross-file integrity: '+repr([r['check'] for r in review_result['checks'] if not r['passed']]),review_result['passed'])
    if args.verify_hashes:
        hashes=load('SHA256SUMS.json')
        check('delivery SHA256 match',all((root/p).is_file() and hashlib.sha256((root/p).read_bytes()).hexdigest()==h for p,h in hashes['files'].items()))
    result={'passed_checks':len(checks),'checks':checks,'source_records':len(sources),'unique_source_urls':len({s['url'] for s in sources}),
            'paper_count':len(papers),'unit_test_cases':counts,'author_unit_test_cases':sum(counts.values()),
            'independent_alignment_unit_test_cases':16,'total_author_and_independent_unit_test_cases':sum(counts.values())+16,
            'review_v32_static_checks':review_result['checks_total'],'review_v32_substantive_units':review_result['substantive_units'],
            'review_v32_decisions':review_result['decision_count'],'reference_v32_items':review_result['paper_reference_items'],
            'api_operations':len(opids),'production_behavior_validated':False}
    (root/'verification').mkdir(exist_ok=True)
    (root/'verification/package_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

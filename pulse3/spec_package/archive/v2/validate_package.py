from pathlib import Path
import ast,json,re,collections
root=Path(__file__).resolve().parent
issues=[]
json_files=list(root.rglob('*.json'))
data={str(p.relative_to(root)):json.loads(p.read_text()) for p in json_files}
sources=data['research/source_index.json']; source_ids={x['id'] for x in sources}
assert len(source_ids)==len(sources)==137
assert len({x['url'] for x in sources})==133
papers=data['research/papers_registry.json']['papers']; paper_ids={x['paper_id'] for x in papers}
assert len(papers)==len(paper_ids)==38
assert all(x['reproduction_status']=='not_started' and x['implementation_status']=='not_implemented' for x in papers)
assert all(x.get('publication_status_code') and x.get('publication_status_note') for x in papers)
assert all('publication_status' not in x for x in papers)
assert collections.Counter(x['read_depth'] for x in papers)=={'targeted_fulltext':29,'abstract_only':7,'abstract_and_publisher_excerpt':2}
hypotheses=data['research/hypotheses.json']['hypotheses']
assert {x['paper_ref'] for x in hypotheses}==paper_ids
assert all(x['profitability_validated'] is False and x['status']=='specified_not_implemented' for x in hypotheses)
platforms=data['research/platforms.json']; feature_ids={x['feature_id'] for x in platforms['records']}
assert len(platforms['platforms'])==10 and len(platforms['records'])==len(feature_ids)==56
assert all(x['source_id'] in source_ids for x in platforms['records'])
assert len(data['contracts/definitions.schema.json']['$defs'])==37
inv=data['traceability/code_inventory.json']; syms=data['traceability/symbol_inventory.json']
assert len(inv['files'])==inv['tracked_files']==435
assert len(syms['symbols'])==inv['symbol_declarations']==3484
assert sum(inv['review_counts'].values())==435
for p in [*root.glob('spec/*.md'),root/'contracts/V2_PROTOCOLS.md',root/'Pulse_Ashare_Rewrite_Full.md']:
    ids=set(re.findall(r'\[([A-Z]{1,3}\d{2})\]',p.read_text()))
    unknown=ids-source_ids
    if unknown: issues.append({'path':str(p.relative_to(root)),'unresolved_sources':sorted(unknown)})
assert not issues,issues
new_scenarios=set(re.findall(r'^\| (V2-\d\d) \|',(root/'spec/V2_SYSTEM_DESIGN.md').read_text(),re.M))
assert len(new_scenarios)==27
base_scenarios=set(re.findall(r'\bV\d\d\b',(root/'spec/GREENFIELD_SPEC.md').read_text()))
assert len(base_scenarios)==22
counts={}
for filename in ['test_feature_store.py','test_account_model.py']:
    tree=ast.parse((root/'reference_models'/filename).read_text())
    counts[filename]=sum(isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name.startswith('test_') for x in ast.walk(tree))
assert counts=={'test_feature_store.py':19,'test_account_model.py':26}
plan_states=data['contracts/definitions.schema.json']['$defs']['TradePlan']['properties']['state']['enum']
assert set(plan_states)=={'watching','actionable','holding','superseded','expired'}
protocol=(root/'contracts/V2_PROTOCOLS.md').read_text()
assert 'draft、compiled、active、superseded' not in protocol
assert 'watching、actionable、holding、superseded、expired' in protocol
reasons=data['traceability/reason_records.json']['records']
requirements=data['traceability/requirements_map.json']['requirements']
reason_ids={r['reason_id'] for r in reasons}
assert len(reasons)==len(reason_ids)==len(requirements)==24
assert all(set(r['reason_ids'])<=reason_ids for r in requirements)
for r in requirements:
    assert set(r['source_refs'])<=source_ids
    assert set(r['paper_refs'])<=paper_ids
    assert set(r['platform_feature_refs'])<=feature_ids
for r in reasons:
    if r['status']=='implemented_reference':
        assert (root/r['component_path']).is_file()
    else:
        assert 'planned_component' in r and 'component_path' not in r
def check_refs(value):
    if isinstance(value,list):
        for v in value: check_refs(v)
    elif isinstance(value,dict):
        if 'path' in value:
            p=root/value['path']; assert p.is_file(),p
            if 'heading' in value: assert value['heading'] in p.read_text(),value
            if 'json_pointer' in value:
                current=json.loads(p.read_text())
                for part in value['json_pointer'].strip('/').split('/'):
                    part=part.replace('~1','/').replace('~0','~')
                    current=current[int(part)] if isinstance(current,list) else current[part]
        for v in value.values(): check_refs(v)
check_refs(reasons); check_refs(requirements)
out={'status':'pass','json_files_parsed':len(json_files),'source_ids':len(sources),'unique_source_urls':133,'papers':38,'paper_hypotheses':38,'platform_capabilities':56,'files_indexed':435,'symbol_declarations':3484,'reason_records':24,'requirements':24,'design_scenarios':49,'reference_test_methods':counts,'citation_closure':'pass','reason_reference_closure':'pass','tradeplan_states':plan_states,'scope':'Structural and cross-reference checks only; reference behavior results are in reference_models/validation.log. No profitability validation or full website execution.'}
print(json.dumps(out,ensure_ascii=False,indent=2))

"""Static checks only. This script never connects to PostgreSQL or a provider."""
from pathlib import Path
from copy import deepcopy
import argparse,hashlib,json,re,sys
from importlib.metadata import version
from pglast import ast,parse_sql,parse_plpgsql
from pglast.stream import RawStream
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).parent
p=argparse.ArgumentParser();p.add_argument('--contracts-dir',type=Path,default=ROOT.parent/'contracts');p.add_argument('--schema-root',type=Path,default=ROOT.parent);args=p.parse_args()
CONTRACTS=args.contracts_dir
sql_paths=[args.schema_root/x for x in ['database/core_schema.sql','execution/ledger_schema.sql','sources_sectors/sources_sectors_schema.sql','integration/integration_schema.sql']]
all_tables={};statements=[];file_reports=[];function_reports=[]
def emit(node):return RawStream()(node)
def walk(node):
 if isinstance(node,ast.Node):
  yield node
  for attr in node:yield from walk(getattr(node,attr))
 elif isinstance(node,(tuple,list)):
  for n in node:yield from walk(n)
for path in sql_paths:
 raw=path.read_text();parsed=parse_sql(raw);statements.extend(parsed)
 count=0
 for q in parsed:
  t=q.stmt
  if isinstance(t,ast.CreateStmt):
   name=t.relation.schemaname+'.'+t.relation.relname
   assert name not in all_tables,('duplicate table',name)
   all_tables[name]=t;count+=1
  if isinstance(t,ast.CreateFunctionStmt):
   name='.'.join(x.sval for x in t.funcname)
   try:
    parse_plpgsql(emit(t));function_reports.append({'function':name,'status':'syntax_parsed_without_catalog'})
   except Exception as exc:
    if 'LookupExplicitNamespace only supports pg_catalog and public' not in str(exc):raise
    function_reports.append({'function':name,'status':'not_resolved_by_static_parser','reason':'pglast cannot resolve custom-schema composite/domain/%ROWTYPE declarations without a catalog; SQL CREATE FUNCTION statement parsed, body not claimed validated.'})
 file_reports.append({'path':str(path.relative_to(args.schema_root)),'statements':len(parsed),'tables':count,'sha256':hashlib.sha256(raw.encode()).hexdigest()})
# Confirm that every declared FK points at an existing table and existing named target columns.
fk_count=0
for q in statements:
 for c in walk(q.stmt):
  if isinstance(c,ast.Constraint) and c.contype.name=='CONSTR_FOREIGN':
   target=(c.pktable.schemaname or 'public')+'.'+c.pktable.relname
   assert target in all_tables,('missing FK target',target)
   cols={x.colname for x in all_tables[target].tableElts or [] if isinstance(x,ast.ColumnDef)}
   for name in c.pk_attrs or []:assert name.sval in cols,('missing FK column',target,name.sval)
   fk_count+=1
# Generate the integration-only field dictionary from actual SQL syntax and inline comments.
raw=sql_paths[-1].read_text();comments={};current=None
for line in raw.splitlines():
 m=re.match(r'CREATE TABLE ([a-z_]+\.[a-z_]+) \(',line)
 if m:current=m.group(1)
 elif current and line.startswith(')'):current=None
 elif current and '--' in line:
  declaration,comment=line.split('--',1);m=re.match(r'\s+([a-z_]+)\s+',declaration)
  if m:comments[(current,m.group(1))]=comment.strip()
integration_tables=[]
for q in parse_sql(raw):
 t=q.stmt
 if not isinstance(t,ast.CreateStmt):continue
 name=t.relation.schemaname+'.'+t.relation.relname
 cons=[x for x in t.tableElts if isinstance(x,ast.Constraint)]
 pk={k.sval for c in cons if c.contype.name=='CONSTR_PRIMARY' for k in c.keys or []}
 fields=[]
 for c in t.tableElts:
  if not isinstance(c,ast.ColumnDef):continue
  typ=emit(c.typeName);flags={x.contype.name for x in c.constraints or []}
  wire='string' if typ in ['uuid','timestamptz','date','text','char(3)','core.sha256_hex','core.finite_decimal','core.nonnegative_amount','core.price_raw'] else 'integer' if typ in ['integer','bigint'] else 'boolean' if typ=='boolean' else 'object'
  f={'name':c.colname,'postgres_type':typ,'json_type':wire,'nullable':not ('CONSTR_NOTNULL' in flags or 'CONSTR_PRIMARY' in flags or c.colname in pk),'description_zh':comments.get((name,c.colname)),'constraints':[emit(x) for x in c.constraints or []]}
  assert f['description_zh'],(name,c.colname,'missing field explanation')
  fields.append(f)
 integration_tables.append({'table':name,'fields':fields,'constraints':[emit(c) for c in cons]})
field_dictionary={'schema_version':'3.0.0','scope':'Integration seed fields only; core/source/execution dictionaries remain the authorities for their own columns.','canonical_contract':'../contracts/v3.schema.json','field_count':sum(len(t['fields']) for t in integration_tables),'tables':integration_tables,'cross_domain_fk_additions':['decision.trade_plan.account_id → execution.account_head.account_id','decision.trade_plan.(account_id,expected_account_version) → execution.account_event.(account_id,account_sequence)','execution journal_line/position_projection/quantity_lot/order_reservation.instrument_id → core.instrument.instrument_id','decision.actionability_snapshot.(ranking_id,release_id) → decision.ranking_run.(ranking_id,release_id)'],'numeric_policy':'Amounts, prices and finite ratios use numeric(38,12) and decimal strings; validate range/scale before DB casts.','time_policy':'available_at is actual completion; policy_usable_at is derived under a versioned query/manifest policy and never overwrites source timestamps.'}
(ROOT/'field_dictionary.json').write_text(json.dumps(field_dictionary,ensure_ascii=False,indent=2)+'\n')
# Consolidated actual SQL dictionary. Do not invent domain semantics where source documentation is absent.
known_fields={}
for relative in ['database/field_dictionary.json','execution/field_dictionary.json','sources_sectors/sources_sectors_fields.json','integration/field_dictionary.json']:
 source_path=args.schema_root/relative
 if not source_path.exists():continue
 source_data=json.loads(source_path.read_text())
 for table in source_data.get('tables',[]):
  for field in table.get('fields',[]):
   field_name=field.get('name',field.get('field'))
   known_fields[(table['table'],field_name)]={'description':field.get('description_zh',field.get('meaning')),'unit':field.get('unit'),'semantic_source':field.get('semantic_source') or relative,'dictionary_file':relative}
all_comments={};table_files={};comment_statements={};foreign_keys={}
def primary_columns(table):
 result=[]
 for item in table.tableElts or []:
  if isinstance(item,ast.ColumnDef) and any(c.contype.name=='CONSTR_PRIMARY' for c in item.constraints or []):result.append(item.colname)
  elif isinstance(item,ast.Constraint) and item.contype.name=='CONSTR_PRIMARY':result.extend(x.sval for x in item.keys or [])
 return result

def record_fk(table_name,c,local_columns=None):
 target=(c.pktable.schemaname or 'public')+'.'+c.pktable.relname
 local_columns=local_columns or [x.sval for x in c.fk_attrs or []]
 target_columns=[x.sval for x in c.pk_attrs or []] or primary_columns(all_tables[target])
 foreign_keys.setdefault(table_name,[]).append({'columns':local_columns,'references_table':target,'references_columns':target_columns,'constraint_sql':emit(c)})
for path in sql_paths:
 current=None
 for line in path.read_text().splitlines():
  m=re.match(r'CREATE TABLE ([a-z_]+\.[a-z_]+) \(',line)
  if m:current=m.group(1);table_files[current]=str(path.relative_to(args.schema_root))
  elif current and line.startswith(')'):current=None
  elif current and '--' in line:
   declaration,comment=line.split('--',1);m=re.match(r'\s+([a-z_]+)\s+',declaration)
   if m:all_comments[(current,m.group(1))]=comment.strip()
 for q in parse_sql(path.read_text()):
  t=q.stmt
  if isinstance(t,ast.CommentStmt) and t.comment:
   names=[x.sval for x in t.object or [] if isinstance(x,ast.String)]
   if len(names)==3:comment_statements[(names[0]+'.'+names[1],names[2])]=t.comment
  if isinstance(t,ast.CreateStmt):
   table_name=t.relation.schemaname+'.'+t.relation.relname
   for item in t.tableElts or []:
    if isinstance(item,ast.ColumnDef):
     for c in item.constraints or []:
      if c.contype.name=='CONSTR_FOREIGN':record_fk(table_name,c,[item.colname])
    elif isinstance(item,ast.Constraint) and item.contype.name=='CONSTR_FOREIGN':record_fk(table_name,item)
  elif isinstance(t,ast.AlterTableStmt):
   table_name=t.relation.schemaname+'.'+t.relation.relname
   for command in t.cmds or []:
    for c in walk(command):
     if isinstance(c,ast.Constraint) and c.contype.name=='CONSTR_FOREIGN':record_fk(table_name,c)
all_field_tables=[];missing_descriptions=[]
for table_name,table in all_tables.items():
 primary=set(primary_columns(table));fields=[]
 for column in table.tableElts or []:
  if not isinstance(column,ast.ColumnDef):continue
  flags={x.contype.name for x in column.constraints or []};typ=emit(column.typeName)
  known=known_fields.get((table_name,column.colname),{})
  description=known.get('description') or all_comments.get((table_name,column.colname)) or comment_statements.get((table_name,column.colname))
  semantic_source=known.get('semantic_source') if known.get('description') else table_files[table_name]+' inline SQL comment' if (table_name,column.colname) in all_comments else table_files[table_name]+' COMMENT ON COLUMN' if (table_name,column.colname) in comment_statements else None
  unit=known.get('unit');unit_source=semantic_source if unit else None
  if unit is None and column.colname.endswith('_cny'):
   unit='CNY';unit_source='Explicit canonical field suffix plus declared v3 numeric/currency policy; '+table_files[table_name]
  if unit is None and column.colname.endswith('_shares'):
   unit='shares';unit_source='Explicit canonical field suffix; '+table_files[table_name]
  if unit is None and column.colname.endswith('_seconds'):
   unit='seconds';unit_source='Explicit field suffix; '+table_files[table_name]
  if unit is None and column.colname in ['source_event_time','event_time','decision_as_of','analysis_as_of','created_at','updated_at','available_at','recorded_at','published_at'] and typ=='timestamptz':
   unit='UTC instant';unit_source='v3 time policy; source/availability meanings remain distinct'
  if unit is None and table_name=='research.metric_value' and column.colname in ['value','lower_bound','upper_bound']:
   unit='same-row unit, under metric_definition_version';unit_source='research.metric_value.unit and versioned metric contract'
  if unit is None and table_name=='intelligence.funding_observation' and column.colname=='canonical_value':
   unit='same-row canonical_unit and currency';unit_source='intelligence.funding_observation declared unit fields'
  if not description:missing_descriptions.append(table_name+'.'+column.colname)
  defaults=[emit(c.raw_expr) for c in column.constraints or [] if c.contype.name=='CONSTR_DEFAULT']
  fields.append({'name':column.colname,'postgres_type':typ,'nullable':not ('CONSTR_NOTNULL' in flags or 'CONSTR_PRIMARY' in flags or column.colname in primary),'default_sql':defaults[0] if defaults else None,'primary_key_member':column.colname in primary,'column_constraints':[emit(c) for c in column.constraints or []],'foreign_keys':[fk for fk in foreign_keys.get(table_name,[]) if column.colname in fk['columns']],'description_zh':description,'description_status':'documented' if description else 'needs_domain_documentation','semantic_source':semantic_source,'unit':unit,'unit_source':unit_source,'unit_status':'specified' if unit else 'not_assumed; consult column/domain contract if dimensional'})
 all_field_tables.append({'table':table_name,'sql_source':table_files[table_name],'field_count':len(fields),'primary_key_columns':list(primary_columns(table)),'table_constraints':[emit(c) for c in table.tableElts or [] if isinstance(c,ast.Constraint)],'foreign_keys':foreign_keys.get(table_name,[]),'fields':fields})
all_fields={'schema_version':'3.0.0','checked_at':'2026-11-10','table_count':len(all_field_tables),'field_count':sum(t['field_count'] for t in all_field_tables),'scope':'All actual columns from the four SQL seeds, including the execution domain and ALTER TABLE cross-domain foreign keys. No inference of an unavailable business definition or currency.','type_and_null_default_fk_source':'Parsed SQL AST, not prose generation.','description_sources':['database/field_dictionary.json','execution/field_dictionary.json','sources_sectors/sources_sectors_fields.json','integration/field_dictionary.json','inline SQL comments and COMMENT ON COLUMN'],'fields_needing_domain_documentation':missing_descriptions,'tables':all_field_tables}
(ROOT/'all_fields.json').write_text(json.dumps(all_fields,ensure_ascii=False,indent=2)+'\n')

# Validate schemas, all local references, operation mapping and physical table existence.
schema=json.loads((CONTRACTS/'v3.schema.json').read_text());oas=json.loads((CONTRACTS/'openapi.json').read_text());bindings=json.loads((CONTRACTS/'api_bindings.json').read_text())
Draft202012Validator.check_schema(schema)
def check_refs(value,root,prefix):
 if isinstance(value,dict):
  for k,v in value.items():
   if k=='$ref' and v.startswith('#/'):
    n=root
    for seg in v[2:].split('/'):n=n[seg.replace('~1','/').replace('~0','~')]
   else:check_refs(v,root,prefix)
 elif isinstance(value,list):
  for x in value:check_refs(x,root,prefix)
check_refs(schema,schema,'schema');check_refs(oas,oas,'openapi')
openapi_state='not_executed'
try:
 from openapi_spec_validator import validate_spec
 validate_spec(oas);openapi_state='passed_openapi_spec_validator_'+version('openapi-spec-validator')
except ImportError:openapi_state='not_executed_dependency_missing'
ops={x['operation_id']:x for x in bindings['operations']};assert len(ops)==len(bindings['operations'])
seen=set()
for path,methods in oas['paths'].items():
 for method,op in methods.items():
  operation_id=op['operationId'];assert operation_id in ops;seen.add(operation_id)
  binding=ops[operation_id];assert path==binding['path'] and method.upper()==binding['method']
  assert op['x-application-service']==binding['application_service']
  for table in binding['tables']:assert table in all_tables,('API references missing table',operation_id,table)
  assert operation_id+'Response' in schema['$defs']
  assert binding['response_type'] in schema['$defs']
  if binding['request_type']:assert binding['request_type'] in schema['$defs']
assert seen==set(ops)
expected_feature=['feature_id','definition_version','implementation_digest','parameter_digest','instrument_ids','timeframe','adjustment_mode','snapshot_id','decision_as_of','availability_policy_version','session_policy_version','input_revision','input_schema_version','dependency_digest','universe_policy_version','calendar_version','adjustment_version','market_rules_version','numeric_policy_version']
assert set(schema['$defs']['FeatureKey']['properties'])==set(expected_feature)
assert set(schema['$defs']['FeatureKey']['required'])==set(expected_feature)
# Small boundary examples validate the contract's important semantic encodings, not an API implementation.
def validator(name):return Draft202012Validator({'$schema':schema['$schema'],'$defs':schema['$defs'],'$ref':'#/$defs/'+name},format_checker=FormatChecker())
def check(name,value,valid):
 errors=list(validator(name).iter_errors(value))
 assert bool(errors) != valid,(name,value,valid,[e.message for e in errors])
check_count=0
for name,value,valid in [('Decimal','99999999999999999999999999.999999999999',True),('Decimal','100000000000000000000000000',False),('Decimal','0.0000000000001',False),('Decimal','1e3',False),('Decimal',1.2,False),('PositiveDecimal','0.000',False),('PositiveDecimal','0.001',True),('Probability','1',True),('OpenUnitInterval','1',False),('OpenUnitInterval','0',False),('OpenUnitInterval','0.95',True),('UUID','XSHG:600000',False),('Timestamp','2026-11-10T02:00:00Z',True),('Timestamp','2026-11-10T10:00:00+08:00',False),('PlanState','draft',False),('PlanState','actionable',True),('Clock',{'kind':'bar_close'},False),('Clock',{'kind':'bar_close','timeframe':'1d','session_policy_version':'cn-v1'},True),('PlanValidation',{'status':'passed','policy_version':'v1','evaluation_id':None,'result_ref':None,'validated_at':None},False),('PlanValidation',{'status':'passed','policy_version':'v1','evaluation_id':None,'result_ref':'artifact://validation/example','validated_at':'2026-11-10T02:00:00Z'},True)]:
 check(name,value,valid);check_count+=1
bar={'instrument_id':'00000000-0000-4000-8000-000000000001','trade_date':'2026-11-10','timeframe':'1m','session_scope':'continuous-v1','bar_open_time':'2026-11-10T02:00:00Z','bar_close_time':'2026-11-10T02:01:00Z','snapshot_id':'00000000-0000-4000-8000-000000000002','currency':'CNY','adjustment_mode':'raw','open_price_raw':'10','high_price_raw':'10.1','low_price_raw':'9.9','close_price_raw':'10.05','volume_shares':1000,'turnover_amount_cny':'10000','exchange_reference_price_raw':None,'is_final':True,'available_at':'2026-11-10T02:01:01Z','quality_status':'valid','field_quality':{}}
check('CanonicalBar',bar,True);check_count+=1
bad=deepcopy(bar);bad['volume_shares']=None;check('CanonicalBar',bad,False);check_count+=1
bad=deepcopy(bar);bad['adjustment_basis']=bad.pop('adjustment_mode');check('CanonicalBar',bad,False);check_count+=1
(ROOT/'contract_examples.json').write_text(json.dumps({'fixture_kind':'synthetic_contract_boundary_example','canonical_bar':bar,'not_live_market_data':True},ensure_ascii=False,indent=2)+'\n')
report={'checked_at':'2026-11-10','sql_parser':'pglast '+version('pglast'),'files':file_reports,'total_tables':len(all_tables),'integration_tables':len(integration_tables),'integration_fields':field_dictionary['field_count'],'all_sql_fields':all_fields['field_count'],'field_descriptions_missing':len(missing_descriptions),'foreign_key_targets_checked':fk_count,'foreign_key_validation_scope':'declared target tables and named target columns exist; no runtime type/index/constraint/concurrency proof','plpgsql':function_reports,'json_schema_meta_validation':'passed','json_schema_version':version('jsonschema'),'openapi_structural_validation':openapi_state,'operations':len(ops),'paths':len(oas['paths']),'schema_definitions':len(schema['$defs']),'all_local_refs_resolve':True,'all_api_table_refs_exist':True,'feature_key_dimensions':19,'contract_boundary_examples_checked':check_count,'postgres_migration_executed':False,'postgres_triggers_executed':False,'production_api_implemented':False,'provider_connections_verified':False,'performance_benchmark_executed':False}
(ROOT/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ['files','plpgsql']},ensure_ascii=False,indent=2))
print('PL/pgSQL functions with unresolved catalog types:',[x['function'] for x in function_reports if x['status']!='syntax_parsed_without_catalog'])

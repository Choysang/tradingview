"""Independent static contract/FK review; never connects to PostgreSQL.

Run with the pinned pglast dependency on PYTHONPATH when it is not installed.
Final target uniqueness/type checks supplement, not replace, real migration tests.
"""
from pathlib import Path
import argparse
import hashlib
import json
from pglast import ast, parse_sql
from pglast.stream import RawStream
from jsonschema import Draft202012Validator, FormatChecker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'final_contract_review.json')
    args = parser.parse_args()
    paths = ['database/core_schema.sql', 'execution/ledger_schema.sql',
             'sources_sectors/sources_sectors_schema.sql', 'integration/integration_schema.sql']
    tables, foreign_keys, unique_keys, primary_keys, statements = {}, [], {}, {}, []
    hashes = {}
    def emit(node):
        return RawStream()(node)

    def table_name(node):
        return (node.schemaname or 'public') + '.' + node.relname

    def strings(values):
        return tuple(item.sval for item in values or [])

    def constraint(owner, node, column=None):
        kind = node.contype.name
        if kind in ('CONSTR_PRIMARY', 'CONSTR_UNIQUE'):
            keys = strings(node.keys) or (column,)
            unique_keys.setdefault(owner, set()).add(keys)
            if kind == 'CONSTR_PRIMARY':
                primary_keys[owner] = keys
        elif kind == 'CONSTR_FOREIGN':
            foreign_keys.append((owner, strings(node.fk_attrs) or (column,),
                                 table_name(node.pktable), strings(node.pk_attrs)))

    for path in paths:
        raw = (args.root / path).read_bytes()
        hashes[path] = hashlib.sha256(raw).hexdigest()
        statements.extend(parse_sql(raw.decode()))
    for statement in statements:
        node = statement.stmt
        if isinstance(node, ast.CreateStmt):
            name = table_name(node.relation)
            assert name not in tables, ('duplicate table', name)
            columns = {}
            for item in node.tableElts or []:
                if isinstance(item, ast.ColumnDef):
                    columns[item.colname] = emit(item.typeName)
                    for c in item.constraints or []:
                        constraint(name, c, item.colname)
                elif isinstance(item, ast.Constraint):
                    constraint(name, item)
            tables[name] = columns
        elif isinstance(node, ast.AlterTableStmt):
            name = table_name(node.relation)
            for item in node.cmds or []:
                if isinstance(item.def_, ast.Constraint):
                    constraint(name, item.def_)
        elif isinstance(node, ast.IndexStmt) and node.unique and not node.whereClause:
            # Only ordinary complete-column unique indexes are FK candidates.
            if all(item.name and not item.expr for item in node.indexParams):
                unique_keys.setdefault(table_name(node.relation), set()).add(
                    tuple(item.name for item in node.indexParams))

    examined = []
    for source, source_cols, target, target_cols in foreign_keys:
        assert source in tables and target in tables, ('missing table', source, target)
        target_cols = target_cols or primary_keys.get(target, ())
        assert len(source_cols) == len(target_cols) > 0, ('FK arity', source, source_cols, target, target_cols)
        assert target_cols in unique_keys.get(target, set()), ('FK uniqueness', source, target, target_cols)
        for left, right in zip(source_cols, target_cols):
            assert left in tables[source] and right in tables[target], ('FK column', source, left, target, right)
            assert tables[source][left] == tables[target][right], (
                'FK exact declared type mismatch', source, left, tables[source][left], target, right, tables[target][right])
        examined.append({'source': source, 'columns': source_cols, 'target': target, 'target_columns': target_cols})

    dictionary_path = 'integration/all_fields.json'
    dictionary_bytes = (args.root / dictionary_path).read_bytes()
    hashes[dictionary_path] = hashlib.sha256(dictionary_bytes).hexdigest()
    dictionary = json.loads(dictionary_bytes)
    declared_columns = {(table, name) for table, columns in tables.items() for name in columns}
    documented_columns = set()
    for table in dictionary['tables']:
        for field in table['fields']:
            identity = table['table'], field['name']
            assert identity not in documented_columns
            documented_columns.add(identity)
            assert tables[identity[0]][identity[1]] == field['postgres_type']
            assert field['description_zh'].strip() and field['description_status'] == 'documented'
    assert declared_columns == documented_columns
    assert dictionary['fields_needing_domain_documentation'] == []

    contract_paths = ['contracts/v3.schema.json', 'contracts/openapi.json', 'contracts/api_bindings.json']
    contract_values = []
    for path in contract_paths:
        raw = (args.root / path).read_bytes()
        hashes[path] = hashlib.sha256(raw).hexdigest()
        contract_values.append(json.loads(raw))
    schema, openapi, bindings = contract_values
    Draft202012Validator.check_schema(schema)

    def refs(value, root):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == '$ref' and item.startswith('#/'):
                    target = root
                    for part in item[2:].split('/'):
                        target = target[part.replace('~1', '/').replace('~0', '~')]
                else:
                    refs(item, root)
        elif isinstance(value, list):
            for item in value:
                refs(item, root)

    refs(schema, schema)
    refs(openapi, openapi)
    # The OpenAPI copy must contain exactly the same DTO rules, not a drifting fork.
    oas_defs = json.loads(json.dumps(openapi['components']['schemas']).replace('#/components/schemas/', '#/$defs/'))
    assert schema['$defs'] == oas_defs
    by_id = {item['operation_id']: item for item in bindings['operations']}
    assert len(by_id) == len(bindings['operations'])
    seen = set()
    for path, methods in openapi['paths'].items():
        for method, op in methods.items():
            if method not in ('get', 'post', 'put', 'delete', 'patch', 'options', 'head', 'trace'):
                continue
            operation = op['operationId']
            assert operation not in seen
            seen.add(operation)
            bind = by_id[operation]
            assert (bind['path'], bind['method']) == (path, method.upper())
            assert bind['application_service'] == op['x-application-service']
            assert all(table in tables for table in bind['tables'])
            response = schema['$defs'][operation + 'Response']
            assert response['properties']['data']['$ref'] == '#/$defs/' + bind['response_type']
    assert seen == set(by_id)

    # Adversarial numeric/enum cases selected from contract and SQL boundaries.
    boundary = [
        ('Decimal', '99999999999999999999999999.999999999999', True),
        ('Decimal', '100000000000000000000000000', False),
        ('Decimal', '0.0000000000001', False), ('Decimal', '1e2', False),
        ('Decimal', 1.25, False), ('NonnegativeDecimal', '-0', False),
        ('NonnegativeDecimal', '0', True), ('Probability', '1', True),
        ('OpenUnitInterval', '1', False), ('OpenUnitInterval', '0', False),
        ('OpenUnitInterval', '0.95', True), ('SafeInteger', 9007199254740992, False),
        ('Timestamp', '2026-11-10T01:59:00Z', True),
        ('Timestamp', '2026-11-10T09:59:00+08:00', False),
        ('PlanState', 'draft', False), ('PlanState', 'actionable', True),
        ('Clock', {'kind': 'bar_close'}, False),
        ('Clock', {'kind': 'bar_close', 'timeframe': '1m', 'session_policy_version': 'fixture'}, True),
    ]
    for name, value, valid in boundary:
        definition = {'$schema': schema['$schema'], '$defs': schema['$defs'], '$ref': '#/$defs/' + name}
        errors = list(Draft202012Validator(definition, format_checker=FormatChecker()).iter_errors(value))
        assert bool(errors) != valid, (name, value, valid)
    report = {
        'review_kind': 'independent_static_only', 'tables': len(tables),
        'foreign_keys_checked': len(examined),
        'fk_scope': 'All target/source columns exist, arity and exact declared SQL types match, target has final nonpartial primary/unique constraint or ordinary unique index.',
        'schema_definitions': len(schema['$defs']), 'api_operations': len(seen), 'api_paths': len(openapi['paths']),
        'documented_fields_matched_to_actual_sql': len(documented_columns),
        'undocumented_fields': 0,
        'local_refs_resolve': True, 'openapi_dto_rules_identical_to_canonical_schema': True,
        'api_service_and_physical_table_bindings_resolve': True,
        'independent_boundary_examples': len(boundary),
        'postgres_migration_executed': False, 'trigger_or_concurrency_execution': False,
        'runtime_handler_coverage_claimed': False, 'sha256': hashes,
        'foreign_keys': examined,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'PASS independent static review: {len(tables)} tables; {len(examined)} FK columns/types/unique targets; '
          f'{len(seen)} API operations; {len(documented_columns)} documented fields; {len(boundary)} contract boundaries.')
    print('No PostgreSQL migration, triggers, concurrency or HTTP implementation were executed.')


if __name__ == '__main__':
    main()

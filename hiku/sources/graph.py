from .. import query
from ..dsl import to_expr
from ..refs import RequirementsExtractor
from ..graph import Link, Field
from ..query import merge
from ..engine import Query, store_fields
from ..checker import check
from ..compiler import ExpressionCompiler


THIS_LINK_NAME = '__link_to_this'


def _create_result_proc(query, env, edge, fields, field_procs, ids):
    def result_proc(result):
        sq_result = query.result()
        store_fields(result, edge, fields, ids, [
            [field_proc(this, env, sq_result) for field_proc in field_procs]
            for this in sq_result[THIS_LINK_NAME]
        ])
    return result_proc


def subquery_fields(sub_root, sub_edge_name, exprs):
    re_env = {}
    exprs = {name: to_expr(obj, re_env) for name, obj in exprs.items()}
    ec_env = {func.__fn_name__ for func in re_env.values()}
    fn_env = {func.__fn_name__: func.fn for func in re_env.values()}

    re_env['this'] = sub_root.fields[sub_edge_name]
    re_env.update(sub_root.fields)

    ec = ExpressionCompiler(ec_env)
    reqs_map = {}
    procs_map = {}
    for name, expr in exprs.items():
        expr = check(re_env, expr)
        reqs_map[name] = RequirementsExtractor.extract(re_env, expr)
        procs_map[name] = eval(compile(ec.compile_lambda_expr(expr),
                                       '<expr>', 'eval'))

    def query_func(queue, task_set, edge, fields, ids):
        this_link = Link(THIS_LINK_NAME, None, sub_edge_name, None,
                         to_list=True)

        reqs = merge(reqs_map[f.name] for f in fields)
        procs = [procs_map[f.name] for f in fields]

        this_req = reqs.fields['this'].edge
        other_reqs = query.Edge([r for r in reqs.fields.values()
                                 if r.name != 'this'])

        q = Query(queue, task_set, sub_root)
        q.process_link(sub_root, this_link, this_req, None, ids)
        q.process_edge(sub_root, other_reqs, None)
        return _create_result_proc(q, fn_env, edge, fields, procs, ids)

    query_func.__subquery__ = True

    return [Field(name, query_func) for name in exprs.keys()]

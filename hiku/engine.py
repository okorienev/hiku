import hashlib
import inspect
import warnings
import dataclasses

from copy import deepcopy
from typing import (
    Any,
    TypeVar,
    Callable,
    cast,
    Iterator,
    Tuple,
    Union,
    Dict,
    List,
    NoReturn,
    Optional,
    DefaultDict,
    Awaitable,
)
from functools import partial
from itertools import chain, repeat
from collections import (
    defaultdict,
    deque,
)
from collections.abc import Sequence, Mapping, Hashable

from .cache import BaseCache
from .compat import Concatenate, ParamSpec
from .executors.base import SyncAsyncExecutor
from .query import (
    Node as QueryNode,
    Field as QueryField,
    Link as QueryLink,
    QueryTransformer,
    QueryVisitor
)
from .graph import (
    Link,
    Maybe,
    One,
    Many,
    Nothing,
    Field,
    Graph,
    Node,
)
from .result import (
    Proxy,
    Index,
    ROOT,
    Reference,
)
from .executors.queue import (
    Workflow,
    Queue,
    TaskSet,
    SubmitRes,
)


def _yield_options(
    graph_obj: Union[Link, Field],
    query_obj: Union[QueryField, QueryLink]
) -> Iterator[Tuple[str, Any]]:
    options = query_obj.options or {}
    for option in graph_obj.options:
        value = options.get(option.name, option.default)
        if value is Nothing:
            raise TypeError('Required option "{}" for {!r} was not provided'
                            .format(option.name, graph_obj))
        else:
            yield option.name, value


def _get_options(
    graph_obj: Union[Link, Field],
    query_obj: Union[QueryField, QueryLink]
) -> Dict:
    return dict(_yield_options(graph_obj, query_obj))


class InitOptions(QueryTransformer):

    def __init__(self, graph: Graph) -> None:
        self._graph = graph
        self._path = [graph.root]

    def visit_field(self, obj: QueryField) -> QueryField:
        graph_obj = self._path[-1].fields_map[obj.name]
        if graph_obj.options:
            return obj.copy(options=_get_options(graph_obj, obj))
        else:
            return obj

    def visit_link(self, obj: QueryLink) -> QueryLink:
        graph_obj = self._path[-1].fields_map[obj.name]

        if isinstance(graph_obj, Link):
            self._path.append(self._graph.nodes_map[graph_obj.node])
            try:
                node = self.visit(obj.node)
            finally:
                self._path.pop()
        else:
            assert isinstance(graph_obj, Field), type(graph_obj)
            node = obj.node

        options = _get_options(graph_obj, obj) if graph_obj.options else None
        return obj.copy(node=node, options=options)


# query.Link is considered a complex Field if present in tuple
FieldGroup = Tuple[Field, Union[QueryField, QueryLink]]
CallableFieldGroup = Tuple[Callable, Field, Union[QueryField, QueryLink]]
LinkGroup = Tuple[Link, QueryLink]


class SplitQuery(QueryVisitor):

    def __init__(self, graph_node: Node) -> None:
        self._node = graph_node
        self._fields: List[CallableFieldGroup] = []
        self._links: List[LinkGroup] = []

    def split(
        self, query_node: QueryNode
    ) -> Tuple[List[CallableFieldGroup], List[LinkGroup]]:
        for item in query_node.fields:
            self.visit(item)
        return self._fields, self._links

    def visit_node(self, obj: QueryNode) -> NoReturn:
        raise ValueError('Unexpected value: {!r}'.format(obj))

    def visit_field(self, obj: QueryField) -> None:
        graph_obj = self._node.fields_map[obj.name]
        func = getattr(graph_obj.func, '__subquery__', graph_obj.func)
        self._fields.append((func, graph_obj, obj))

    def visit_link(self, obj: QueryLink) -> None:
        graph_obj = self._node.fields_map[obj.name]
        if isinstance(graph_obj, Link):
            if graph_obj.requires:
                self.visit(QueryField(graph_obj.requires))
            self._links.append((graph_obj, obj))
        else:
            assert isinstance(graph_obj, Field), type(graph_obj)
            # `obj` here is a link, but this link is treated as a complex field
            func = getattr(graph_obj.func, '__subquery__', graph_obj.func)
            self._fields.append((func, graph_obj, obj))


class GroupQuery(QueryVisitor):

    def __init__(self, node: Node) -> None:
        self._node = node
        self._funcs: List[Callable] = []
        self._groups: List[Union[List[FieldGroup], LinkGroup]] = []
        self._current_func = None

    def group(
        self, node: QueryNode
    ) -> List[Tuple[Callable, Union[List[FieldGroup], LinkGroup]]]:
        for item in node.fields:
            self.visit(item)
        return list(zip(self._funcs, self._groups))

    def visit_node(self, obj: QueryNode) -> NoReturn:
        raise ValueError('Unexpected value: {!r}'.format(obj))

    def visit_field(self, obj: QueryField) -> None:
        graph_obj = self._node.fields_map[obj.name]
        func = getattr(graph_obj.func, '__subquery__', graph_obj.func)
        if func == self._current_func:
            assert isinstance(self._groups[-1], list)
            self._groups[-1].append((graph_obj, obj))
        else:
            self._groups.append([(graph_obj, obj)])
            self._funcs.append(func)
            self._current_func = func

    def visit_link(self, obj: QueryLink) -> None:
        graph_obj = self._node.fields_map[obj.name]
        if graph_obj.requires:
            self.visit(QueryField(graph_obj.requires))
        self._groups.append((graph_obj, obj))
        self._funcs.append(graph_obj.func)
        self._current_func = None


class HashVisitor(QueryVisitor):
    def __init__(self, hasher) -> None:  # type: ignore
        self._hasher = hasher

    def visit_field(self, obj: QueryField) -> None:
        self._hasher.update(obj.index_key.encode())

    def visit_node(self, obj: QueryNode) -> None:
        for item in obj.fields:
            self.visit(item)

    def visit_link(self, obj: QueryLink) -> None:
        self._hasher.update(obj.index_key.encode())
        super().visit_link(obj)


def _check_store_fields(
    node: Node,
    fields: List[Union[QueryField, QueryLink]],
    ids: Optional[Any],
    result: Any
) -> None:
    if node.name is not None:
        assert ids is not None
        if (
            isinstance(result, Sequence)
            and len(result) == len(ids)
            and all(isinstance(r, Sequence) and len(r) == len(fields)
                    for r in result)
        ):
            return
        else:
            expected = ('list (len: {}) of lists (len: {})'
                        .format(len(ids), len(fields)))
    else:
        if isinstance(result, Sequence) and len(result) == len(fields):
            return
        else:
            expected = 'list (len: {})'.format(len(fields))
    raise TypeError('Can\'t store field values, node: {!r}, fields: {!r}, '
                    'expected: {}, returned: {!r}'
                    .format(node.name or '__root__', [f.name for f in fields],
                            expected, result))


def _is_hashable(obj: Any) -> bool:
    if not isinstance(obj, Hashable):
        return False

    try:
        hash(obj)
    except TypeError:
        return False

    return True


def update_index(
    index: Index,
    graph: Graph,
    node: Node,
    ids: Any,
    data: List[Dict],
) -> None:
    # TODO: maybe refactor with __refs__ map
    if node.name is not None:
        assert ids is not None
        node_idx = index[node.name]
        for i, row in zip(ids, data):
            if '__ref__' in row:
                ref = row.pop('__ref__')
                field_name = row.pop('__field_name__')
                node_idx[i][field_name] = ref
                update_index(
                    index, graph, graph.nodes_map[ref.node], [ref.ident], [row]
                )
            else:
                node_idx[i] = row
                for field, value in row.items():
                    if isinstance(value, dict) and '__ref__' in value:
                        ref = value.pop('__ref__')
                        field_name = value.pop('__field_name__')
                        node_idx[i][field_name] = ref
                        update_index(
                            index, graph, graph.nodes_map[ref.node],
                            [ref.ident], [value]
                        )
    else:
        raise NotImplementedError('cache not implemented for root nodes yet')

    return None


def store_fields(
    index: Index,
    node: Node,
    query_fields: List[Union[QueryField, QueryLink]],
    ids: Optional[Any],
    query_result: Any
) -> None:
    if inspect.isgenerator(query_result):
        warnings.warn('Data loading functions should not return generators',
                      DeprecationWarning)
        query_result = list(query_result)

    _check_store_fields(node, query_fields, ids, query_result)

    names = [f.index_key for f in query_fields]
    if node.name is not None:
        assert ids is not None
        node_idx = index[node.name]
        for i, row in zip(ids, query_result):
            node_idx[i].update(zip(names, row))
    else:
        assert ids is None
        index.root.update(zip(names, query_result))

    return None


def link_reqs(
    index: Index,
    node: Node,
    link: Link,
    ids: Any
) -> Any:
    """For a given link, find link `requires` values by ids."""
    if node.name is not None:
        assert ids is not None
        node_idx = index[node.name]
        return [node_idx[i][link.requires] for i in ids]
    else:
        return index.root[link.requires]


def link_ref_maybe(graph_link: Link, ident: Any) -> Optional[Reference]:
    if ident is Nothing:
        return None
    else:
        return Reference(graph_link.node, ident)


def link_ref_one(
    graph_link: Link, ident: Any
) -> Reference:
    assert ident is not Nothing
    return Reference(graph_link.node, ident)


def link_ref_many(
    graph_link: Link, idents: List
) -> List[Reference]:
    return [Reference(graph_link.node, i) for i in idents]


_LINK_REF_MAKER: Dict[Any, Callable] = {
    Maybe: link_ref_maybe,
    One: link_ref_one,
    Many: link_ref_many,
}


HASH_HINT = "\nHint: Consider adding __hash__ method or use hashable type."
DATACLASS_HINT = "\nHint: Use @dataclass(frozen=True) to make object hashable."


def _hashable_hint(obj: Any) -> str:
    if isinstance(obj, Sequence):
        return _hashable_hint(obj[0])

    if (
        dataclasses.is_dataclass(obj)
        and not getattr(obj, dataclasses._PARAMS).frozen  # type: ignore[attr-defined]  # noqa: E501
    ):
        return DATACLASS_HINT

    return HASH_HINT


def _check_store_links(
    node: Node,
    link: Link,
    ids: Any,
    result: Any
) -> None:
    hint = ''
    if node.name is not None and link.requires is not None:
        assert ids is not None
        if link.type_enum is Maybe or link.type_enum is One:
            if isinstance(result, Sequence) and len(result) == len(ids):
                if all(map(_is_hashable, result)):
                    return
                expected = 'list of hashable objects'
                hint = _hashable_hint(result)
            else:
                expected = 'list (len: {})'.format(len(ids))
        elif link.type_enum is Many:
            if (
                isinstance(result, Sequence)
                and len(result) == len(ids)
                and all(isinstance(r, Sequence) for r in result)
            ):
                unhashable = False
                for items in result:
                    if not all(map(_is_hashable, items)):
                        unhashable = True
                        break

                if not unhashable:
                    return
                expected = 'list (len: {}) of lists of hashable objects'.format(len(ids))  # noqa: E501
                hint = _hashable_hint(result)
            else:
                expected = 'list (len: {}) of lists'.format(len(ids))
        else:
            raise TypeError(link.type_enum)
    else:
        if link.type_enum is Maybe or link.type_enum is One:
            if _is_hashable(result):
                return
            expected = 'hashable object'
            hint = _hashable_hint(result)
        elif link.type_enum is Many:
            if isinstance(result, Sequence):
                if all(map(_is_hashable, result)):
                    return
                expected = 'list of hashable objects'
                hint = _hashable_hint(result)
            else:
                expected = 'list'
        else:
            raise TypeError(link.type_enum)
    raise TypeError('Can\'t store link values, node: {!r}, link: {!r}, '
                    'expected: {}, returned: {!r}{}'
                    .format(node.name or '__root__', link.name,
                            expected, result, hint))


def store_links(
    index: Index,
    node: Node,
    graph_link: Link,
    query_link: QueryLink,
    ids: Any,
    query_result: Any
) -> None:
    _check_store_links(node, graph_link, ids, query_result)

    field_val: Callable = _LINK_REF_MAKER[graph_link.type_enum]
    if node.name is not None:
        assert ids is not None
        if graph_link.requires is None:
            query_result = repeat(query_result, len(ids))

        node_idx = index[node.name]
        for i, res in zip(ids, query_result):
            node_idx[i][query_link.index_key] = field_val(graph_link, res)
    else:
        index.root[query_link.index_key] = field_val(graph_link, query_result)

    return None


def link_result_to_ids(
    from_list: bool,
    link_type: Any,  # Const
    result: Any
) -> List:
    if from_list:
        if link_type is Maybe:
            return [i for i in result if i is not Nothing]
        elif link_type is One:
            if any(i is Nothing for i in result):
                raise TypeError('Non-optional link should not return Nothing: '
                                '{!r}'.format(result))
            return result
        elif link_type is Many:
            return list(chain.from_iterable(result))
    else:
        if link_type is Maybe:
            return [] if result is Nothing else [result]
        elif link_type is One:
            if result is Nothing:
                raise TypeError('Non-optional link should not return Nothing')
            return [result]
        elif link_type is Many:
            return result
    raise TypeError(repr([from_list, link_type]))


def get_cached_link_ids(
    cache: BaseCache,
    query_link: QueryLink,
    reqs: List,
) -> tuple:
    keys = set()
    key_to_req = {}
    req_to_key = {}
    for req in reqs:
        key = get_query_hash(query_link, req)
        keys.add(key)
        key_to_req[key] = req
        req_to_key[req] = key

    cached_data = cache.get_many(list(keys))

    cached_reqs = []
    cached_results = []
    not_cached_reqs = []

    for req in reqs:
        key = req_to_key[req]
        if key in cached_data:
            cached_reqs.append(req)
            # TODO: suboptimal, try not to copy
            data = deepcopy(cached_data[key])
            cached_results.append(data)
        else:
            not_cached_reqs.append(req)

    return cached_reqs, cached_results, not_cached_reqs


class CacheVisitor(QueryVisitor):
    """Visit cached query link to extract all data from index
    that needs to be cached
    """
    def __init__(self, index: Index, node: Node) -> None:  # type: ignore
        self._index = index
        self._node = node
        self._req = deque()
        self._proxy = deque()
        self._data = deque()

    def visit_field(self, field: QueryField) -> None:
        value = self._proxy[-1][field.name]
        self._data[-1][field.index_key] = value
        super().visit_field(field)

    def visit_node(self, node: QueryNode) -> None:
        for item in node.fields:
            self.visit(item)

    def visit_link(self, link: QueryLink) -> None:
        self._proxy.append(self._proxy[-1][link.name])
        self._data.append({})
        if isinstance(self._proxy[-1], dict):
            self._data[-1].update(self._proxy[-1])
        else:
            self._data[-1]['__ref__'] = self._proxy[-1].__ref__
            self._data[-1]['__field_name__'] = link.name
        super().visit_link(link)
        self._proxy.pop()

        if len(self._data) > 1:
            self._data[-1][link.name] = self._data.pop()

    def process(self, link: QueryLink, reqs: List) -> Dict:
        to_cache = {}
        # TODO: consider storing part of index as it,
        #  it will be easier to restore it
        graph_obj = self._node.fields_map[link.name]
        for req in reqs:
            self._req.append(req)
            ref = Reference(graph_obj.node, req)
            proxy = Proxy(self._index, ref, link.node)
            self._proxy.append(proxy)
            self._data.append({
                '__ref__': ref,
                # TODO: support reusing cached link for different nodes
                '__field_name__': link.name,
            })
            self.visit(link.node)
            key = get_query_hash(link, req)
            to_cache[key] = self._data.pop()

        return to_cache


NodePath = Tuple[str, ...]
CACHE_VERSION = '1'


def get_query_hash(
    query_link: Union[QueryLink, QueryField],
    required_id: Any
) -> str:
    hasher = hashlib.sha1()
    hash_visitor = HashVisitor(hasher)
    hash_visitor.visit(query_link)

    hasher.update(str(hash(required_id)).encode('utf-8'))
    hasher.update(CACHE_VERSION.encode('utf-8'))
    return hasher.hexdigest()


class Query(Workflow):

    def __init__(
        self,
        queue: Queue,
        task_set: TaskSet,
        graph: Graph,
        query: QueryNode,
        ctx: 'Context',
        cache: BaseCache = None
    ) -> None:
        self._queue = queue
        self._task_set = task_set
        self._graph = graph
        self._query = query
        self._ctx = ctx
        self._index = Index()
        self._cache = cache
        self._parent = deque([{}])
        self._in_progress = defaultdict(int)
        self._done_callbacks = defaultdict(list)
        self._path_callback = {}

    def _track(self, path: NodePath):
        self._in_progress[path] += 1

    def _add_done_callback(self, path, callback):
        self._done_callbacks[path].append(callback)

    def _untrack(self, path: NodePath):
        assert self._in_progress[path] > 0, f"Path {path} is already done"
        self._in_progress[path] -= 1
        if self._is_done(path):
            for callback in self._done_callbacks[path]:
                callback()

    def _is_done(self, path: NodePath) -> bool:
        return self._in_progress[path] == 0

    def _submit(
        self,
        func: Callable,
        *args: Any,
        **kwargs: Any
    ) -> SubmitRes:
        if _do_pass_context(func):
            return self._task_set.submit(func, self._ctx, *args, **kwargs)
        else:
            return self._task_set.submit(func, *args, **kwargs)

    def start(self) -> None:
        self.process_node(tuple(), self._graph.root, self._query, None)

    def result(self) -> Proxy:
        self._index.finish()
        return Proxy(self._index, ROOT, self._query)

    def _process_node_ordered(
        self,
        path: NodePath,
        node: Node,
        query: QueryNode,
        ids: Any
    ) -> None:
        proc_steps = GroupQuery(node).group(query)

        # recursively and sequentially schedule fields and links
        def proc(steps: List) -> None:
            step_func, step_item = steps.pop(0)
            if isinstance(step_item, list):
                self._track(path)
                dep = self._schedule_fields(
                    path, node, step_func, step_item, ids
                )
            else:
                graph_link, query_link = step_item
                self._track(path)
                dep = self._schedule_link(
                    path, node, graph_link, query_link, ids
                )

            if steps:
                self._queue.add_callback(dep, lambda: proc(steps))

        if proc_steps:
            proc(proc_steps)

    def process_node(
        self,
        path: NodePath,
        node: Node,
        query: QueryNode,
        ids: Any,
    ) -> None:
        path = path + (node.name,)
        self._path_callback[path] = lambda: self._untrack(path)

        if query.ordered:
            self._process_node_ordered(path, node, query, ids)
            return

        fields, links = SplitQuery(node).split(query)

        to_func: Dict[str, Callable] = {}
        from_func: DefaultDict[Callable, List[FieldGroup]] = defaultdict(list)
        for func, graph_field, query_field in fields:
            to_func[graph_field.name] = func
            from_func[func].append((graph_field, query_field))

        # schedule fields resolve
        to_dep: Dict[Callable, Union[SubmitRes, TaskSet]] = {}
        for func, func_fields in from_func.items():
            self._track(path)
            to_dep[func] = self._schedule_fields(
                path, node, func, func_fields, ids
            )

        # schedule link resolve
        for graph_link, query_link in links:
            self._track(path)
            schedule = partial(self._schedule_link, path, node,
                               graph_link, query_link, ids)
            if graph_link.requires:
                dep = to_dep[to_func[graph_link.requires]]
                self._queue.add_callback(dep, schedule)
            else:
                schedule()

    def process_link(
        self,
        path: NodePath,
        node: Node,
        graph_link: Link,
        query_link: QueryLink,
        ids: Any,
        result: List
    ) -> None:
        """Store Link.func result in index and Call `process_node` to schedule
        Link's fields and links"""
        store_links(self._index, node, graph_link, query_link, ids, result)
        from_list = ids is not None and graph_link.requires is not None
        to_ids = link_result_to_ids(from_list, graph_link.type_enum, result)
        if to_ids:
            self.process_node(path, self._graph.nodes_map[graph_link.node],
                              query_link.node, to_ids)

        return None

    def _schedule_fields(
        self,
        path: NodePath,
        node: Node,
        func: Callable,
        fields: List[FieldGroup],
        ids: Optional[Any],
    ) -> Union[SubmitRes, TaskSet]:
        query_fields = [qf for _, qf in fields]

        dep: Union[TaskSet, SubmitRes]
        if hasattr(func, '__subquery__'):
            assert ids is not None
            dep = self._queue.fork(self._task_set)
            proc = func(fields, ids, self._queue, self._ctx, dep)
        else:
            if ids is None:
                dep = self._submit(func, query_fields)
            else:
                dep = self._submit(func, query_fields, ids)
            proc = dep.result

        def callback() -> None:
            store_fields(self._index, node, query_fields, ids, proc())
            self._untrack(path)
            if self._is_done(path):
                if path[:-1] in self._path_callback:
                    self._path_callback[path[:-1]]()

        self._queue.add_callback(dep, callback)
        return dep

    def _schedule_link(
        self,
        path: NodePath,
        node: Node,
        graph_link: Link,
        query_link: QueryLink,
        ids: Any
    ) -> SubmitRes:
        """Schedules link (submits Link.func to executor).

        If link has `requires` specified, take required values list from index
        and pass it as first argument to link.func.

        If link has `options`, pass as a first argument if `requires` not
        provided or as second if `requires` provided.

        When Link.func is executed by executor, a `process_link`
        method called with result.
        """
        args = []
        cached_ids = []
        if graph_link.requires:
            reqs = link_reqs(self._index, node, graph_link, ids)

            # TODO: use self._submit to fetch cached data.
            if 'cached' in query_link.directives_map and self._cache:
                cached_reqs, cached_data, not_cached_reqs = get_cached_link_ids(
                    self._cache, query_link, reqs
                )
                for i, req in zip(ids, reqs):
                    if req in cached_reqs:
                        cached_ids.append(i)
                if cached_data:
                    update_index(
                        self._index, self._graph, node, cached_ids, cached_data
                    )
                args.append(not_cached_reqs)
            else:
                args.append(reqs)

        if graph_link.options:
            args.append(query_link.options)

        dep = self._submit(graph_link.func, *args)

        def callback() -> None:
            result = dep.result()

            if inspect.isgenerator(result):
                warnings.warn(
                    'Data loading functions should not return generators',
                    DeprecationWarning
                )
                result = list(result)

            if cached_ids:
                nonlocal ids
                ids = [i for i in ids if i not in cached_ids]

            return self.process_link(
                path, node, graph_link, query_link, ids, result
            )

        self._queue.add_callback(dep, callback)

        def store_link_cache() -> None:
            cached = query_link.directives_map['cached']
            reqs = link_reqs(self._index, node, graph_link, ids)
            cacher = CacheVisitor(self._index, node)
            to_cache = cacher.process(query_link, reqs)

            self._submit(self._cache.set_many, to_cache, cached.ttl)

        if 'cached' in query_link.directives_map and self._cache:
            self._add_done_callback(path + (graph_link.node,), store_link_cache)

        return dep


R = TypeVar('R')
P = ParamSpec('P')


def pass_context(
    func: Callable[P, R]
) -> Callable[Concatenate['Context', P], R]:
    """Decorator to pass context to a function as a first argument.

    Can be used on functions for ``Field`` and ``Link``.
    Can not be used on functions with ``@define`` decorator
    """
    func.__pass_context__ = True  # type: ignore[attr-defined]
    return cast(Callable[Concatenate['Context', P], R], func)


def _do_pass_context(func: Callable) -> bool:
    return getattr(func, '__pass_context__', False)


class Context(Mapping):

    def __init__(self, mapping: Mapping) -> None:
        self.__mapping = mapping

    def __len__(self) -> int:
        return len(self.__mapping)

    def __iter__(self) -> Iterator:
        return iter(self.__mapping)

    def __getitem__(self, item: Any) -> Any:
        try:
            return self.__mapping[item]
        except KeyError:
            raise KeyError('Key {!r} is not specified '
                           'in the query context'.format(item))


class Engine:

    def __init__(
        self,
        executor: SyncAsyncExecutor,
        cache: BaseCache = None,
    ) -> None:
        self.executor = executor
        self.cache = cache

    def execute(
        self,
        graph: Graph,
        query: QueryNode,
        ctx: Optional[Dict] = None
    ) -> Union[Proxy, Awaitable[Proxy]]:
        if ctx is None:
            ctx = {}
        query = InitOptions(graph).visit(query)
        queue = Queue(self.executor)
        task_set = queue.fork(None)
        query_workflow = Query(
            queue, task_set, graph, query, Context(ctx), self.cache
        )
        query_workflow.start()
        return self.executor.process(queue, query_workflow)

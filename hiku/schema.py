from dataclasses import dataclass
from inspect import isawaitable
from typing import Optional, Dict, Union, Any, List, Type, Sequence, Tuple, cast

from hiku.context import (
    ExecutionContext,
    ExecutionContextFinal,
    create_execution_context,
)
from hiku.denormalize.graphql import DenormalizeGraphQL
from hiku.engine import Engine
from hiku.extensions.base_extension import Extension, ExtensionsManager
from hiku.extensions.base_validator import QueryValidator
from hiku.graph import Graph, GraphTransformer, apply
from hiku.introspection.graphql import GraphQLIntrospection
from hiku.merge import QueryMerger
from hiku.operation import OperationType
from hiku.query import Node
from hiku.readers.graphql import parse_query, read_operation
from hiku.result import Proxy
from hiku.validate.query import validate


class GraphQLError(Exception):
    def __init__(self, *, errors: List[str]):
        super().__init__("{} errors".format(len(errors)))
        self.errors = errors


def _run_validation(
    graph: Graph,
    query: Node,
    validators: Optional[Tuple[QueryValidator, ...]] = None,
) -> List[str]:
    errors = validate(graph, query)
    for validator in validators or ():
        errors.extend(validator.validate(query, graph))

    return errors


@dataclass
class ExecutionResult:
    data: Optional[Dict[str, Any]]
    error: Optional[GraphQLError]


class Schema:
    graph: Graph
    mutation: Optional[Graph]
    batching: bool
    introspection: bool

    introspection_cls = GraphQLIntrospection

    def __init__(
        self,
        engine: Engine,
        graph: Graph,
        mutation: Optional[Graph] = None,
        batching: bool = False,
        introspection: bool = True,
        extensions: Optional[
            Sequence[Union[Extension, Type[Extension]]]
        ] = None,
        transformers: Optional[List[GraphTransformer]] = None,
    ):
        self.engine = engine
        self.batching = batching
        self.introspection = introspection
        self.extensions = extensions or []

        if not transformers:
            transformers = []

        execution_context = create_execution_context()
        extensions_manager = ExtensionsManager(
            execution_context=execution_context,
            extensions=self.extensions,
        )
        with extensions_manager.init():
            transformers.extend(execution_context.transformers)

        # introspection transformer must be the last one in chain
        if self.introspection:
            transformers.append(self.introspection_cls(graph, mutation))

        self.graph = apply(graph, transformers)
        if mutation is not None:
            self.mutation = apply(mutation, transformers)
        else:
            self.mutation = None

    def execute_sync(
        self,
        query: Union[str, Node],
        variables: Optional[Dict] = None,
        operation_name: Optional[str] = None,
        context: Optional[Dict] = None,
    ) -> ExecutionResult:
        if isinstance(query, Node):
            execution_context = create_execution_context(
                query=query,
                context=context,
                query_graph=self.graph,
                mutation_graph=self.mutation,
            )
        else:
            execution_context = create_execution_context(
                query=query,
                variables=variables,
                operation_name=operation_name,
                context=context,
                query_graph=self.graph,
                mutation_graph=self.mutation,
            )

        execution_context = cast(ExecutionContextFinal, execution_context)

        extensions_manager = ExtensionsManager(
            execution_context=execution_context,
            extensions=self.extensions,
        )

        try:
            with extensions_manager.operation():
                self._init_execution_context(
                    execution_context, extensions_manager
                )

                with extensions_manager.execution():
                    result = self.engine.execute_context(execution_context)
                    assert isinstance(result, Proxy)
                    execution_context.result = result

                data = DenormalizeGraphQL(
                    execution_context.graph,
                    result,
                    execution_context.operation_type_name,
                ).process(execution_context.query)

            return ExecutionResult(data, None)
        except GraphQLError as e:
            return ExecutionResult(None, e)

    async def execute(
        self,
        query: Union[str, Node],
        variables: Optional[Dict] = None,
        operation_name: Optional[str] = None,
        context: Optional[Dict] = None,
    ) -> ExecutionResult:
        if isinstance(query, Node):
            execution_context = create_execution_context(
                query=query,
                context=context,
                query_graph=self.graph,
                mutation_graph=self.mutation,
            )
        else:
            execution_context = create_execution_context(
                query=query,
                variables=variables,
                operation_name=operation_name,
                context=context,
                query_graph=self.graph,
                mutation_graph=self.mutation,
            )

        execution_context = cast(ExecutionContextFinal, execution_context)

        extensions_manager = ExtensionsManager(
            execution_context=execution_context,
            extensions=self.extensions,
        )

        try:
            with extensions_manager.operation():
                self._init_execution_context(
                    execution_context, extensions_manager
                )

                with extensions_manager.execution():
                    coro = self.engine.execute_context(execution_context)
                    assert isawaitable(coro)
                    result = await coro
                    assert isinstance(result, Proxy)
                    execution_context.result = result

                data = DenormalizeGraphQL(
                    execution_context.graph,
                    result,
                    execution_context.operation_type_name,
                ).process(execution_context.query)

            return ExecutionResult(data, None)
        except GraphQLError as e:
            return ExecutionResult(None, e)

    def _validate(
        self,
        graph: Graph,
        query: Node,
        validators: Optional[Tuple[QueryValidator, ...]] = None,
    ) -> List[str]:
        return _run_validation(graph, query, validators)

    def _init_execution_context(
        self,
        execution_context: ExecutionContext,
        extensions_manager: ExtensionsManager,
    ) -> None:
        with extensions_manager.parsing():
            # do not parse query if query of type Node was provided
            if (
                execution_context.graphql_document is None
                and execution_context.query is None
            ):
                assert execution_context.query_src, "query string not provided"
                execution_context.graphql_document = parse_query(
                    execution_context.query_src
                )

            if execution_context.operation is None:
                # assume that if query of type Node provided, operation
                # populated and we will not go in this branch where
                # graphql_document is None
                assert execution_context.graphql_document is not None
                try:
                    execution_context.operation = read_operation(
                        execution_context.graphql_document,
                        execution_context.variables,
                        execution_context.request_operation_name,
                    )
                except TypeError as e:
                    raise GraphQLError(
                        errors=[
                            "Failed to read query: {}".format(e),
                        ]
                    )

            execution_context.query = execution_context.operation.query
            # save original query before merging to validate it
            original_query = execution_context.query

            merger = QueryMerger(execution_context.graph)
            execution_context.query = merger.merge(execution_context.query)
            execution_context.operation.query = execution_context.query

        op = execution_context.operation
        if op.type not in (OperationType.QUERY, OperationType.MUTATION):
            raise GraphQLError(
                errors=["Unsupported operation type: {!r}".format(op.type)]
            )

        with extensions_manager.validation():
            if execution_context.errors is None:
                execution_context.errors = self._validate(
                    execution_context.graph,
                    original_query,
                    execution_context.validators,
                )

            if execution_context.errors:
                raise GraphQLError(errors=execution_context.errors)

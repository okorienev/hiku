from hiku.graph import (
    Field,
    Link,
    Option,
    Root,
)
from hiku.types import (
    Record,
    Integer,
    String,
    TypeRef,
    Optional,
)
from hiku.utils import listify
from hiku.federation.graph import Graph, FederatedNode
from hiku.federation.directive import Key


def get_by_id(id_, collection):
    for item in collection:
        if item['id'] == id_:
            return item

    return None


@listify
def find_all_by_id(id_, collection, key='id'):
    for item in collection:
        if item[key] == id_:
            yield item


data = {
    'carts': [
        dict(id=1, status='NEW'),
        dict(id=2, status='ORDERED'),
    ],
    'cart_items': [
        dict(id=10, cart_id=1, name='Ipad'),
        dict(id=20, cart_id=2, name='Book'),
        dict(id=21, cart_id=2, name='Pen'),
    ]
}


@listify
def cart_resolver(fields, ids):

    def get_field(f, cart):
        if f.name == 'id':
            return cart['id']
        if f.name == 'status':
            return {
                'id': cart['status'],
                'title': cart['status'].lower(),
            }
    for cart_id in ids:
        cart = get_by_id(cart_id, data['carts'])
        yield [get_field(f, cart) for f in fields]


async def async_cart_resolver(fields, ids):
    return cart_resolver(fields, ids)


def direct_link_id(opts):
    return opts['id']


def ids_resolver(fields, ids):
    return [[id_] for id_ in ids]


async def async_ids_resolver(fields, ids):
    return [[id_] for id_ in ids]


def direct_link(ids):
    return ids


async def async_direct_link(ids):
    return ids


data_types = {
    'Status': Record[{
        'id': Integer,
        'title': String,
    }],
}


def resolve_cart(representations):
    return [r['id'] for r in representations]


GRAPH = Graph([
    FederatedNode('Cart', [
        Field('id', Integer, cart_resolver),
        Field('status', TypeRef['Status'], cart_resolver),
    ], directives=[Key('id')], resolve_reference=resolve_cart),
    Root([
        Link(
            'cart',
            Optional[TypeRef['Cart']],
            ids_resolver,
            requires=None,
            options=[
                Option('id', Integer)
            ],
        ),
    ]),
], data_types=data_types)


ASYNC_GRAPH = Graph([
    FederatedNode('Cart', [
        Field('id', Integer, async_cart_resolver),
        Field('status', TypeRef['Status'], async_cart_resolver),
    ], directives=[Key('id')], resolve_reference=resolve_cart),
    Root([
        Link(
            'cart',
            Optional[TypeRef['Cart']],
            async_ids_resolver,
            requires=None,
            options=[
                Option('id', Integer)
            ],
        ),
    ]),
], data_types=data_types, is_async=True)


def field_resolver(fields, ids):
    ...


def link_resolver(ids):
    ...

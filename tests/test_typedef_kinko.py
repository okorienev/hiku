import difflib
from textwrap import dedent

from hiku.graph import Graph, Edge, Field, Link, Root, Many, One
from hiku.types import Sequence, Mapping, Integer, String, Optional, Record
from hiku.typedef.kinko import dumps


def _(*args, **kwargs):
    raise NotImplementedError


def assert_dumps(root, schema):
    first = dumps(root)
    second = dedent(schema).strip() + '\n'
    if first != second:
        msg = ('Dumped schema mismatches:\n\n{}'
               .format('\n'.join(difflib.ndiff(first.splitlines(),
                                               second.splitlines()))))
        raise AssertionError(msg)


def test_field():
    assert_dumps(
        Graph([Root([
            Field('leones', String, _),
        ])]),
        """
        type leones String
        """,
    )


def test_field_complex():
    assert_dumps(
        Graph([Root([
            Field('behave', Optional[Record[{'burieth': Integer}]], _),
            Field('gemara', Record[{'trevino': Integer}], _),
            Field('riffage', Sequence[Record[{'shophar': Integer}]], _),
        ])]),
        """
        type behave
          Option
            Record
              :burieth Integer

        type gemara
          Record
            :trevino Integer

        type riffage
          List
            Record
              :shophar Integer
        """,
    )


def test_edge():
    assert_dumps(
        Graph([
            Edge('adder', [
                Field('kott', String, _),
                Field('aseptic', String, _),
            ]),
            Edge('brayden', [
                Field('unhot', String, _),
                Field('linea', String, _),
            ]),
        ]),
        """
        type adder
          Record
            :kott String
            :aseptic String

        type brayden
          Record
            :unhot String
            :linea String
        """,
    )


def test_list_simple():
    assert_dumps(
        Graph([Root([
            Field('askest', Sequence[Integer], _),
        ])]),
        """
        type askest
          List Integer
        """,
    )


def test_list_complex():
    assert_dumps(
        Graph([Root([
            Field('gladden', Sequence[Sequence[Integer]], _),
        ])]),
        """
        type gladden
          List
            List Integer
        """,
    )


def test_dict_simple():
    assert_dumps(
        Graph([Root([
            Field('kasseri', Mapping[String, Integer], _),
        ])]),
        """
        type kasseri
          Dict String Integer
        """,
    )


def test_dict_complex():
    assert_dumps(
        Graph([Root([
            Field('trunks', Mapping[String, Mapping[Integer, Integer]], _),
        ])]),
        """
        type trunks
          Dict String
            Dict Integer Integer
        """,
    )


def test_type_ref():
    assert_dumps(
        Graph([
            Edge('xeric', [
                Field('derrida', String, _),
            ]),
            Edge('amb', [
                Field('loor', String, _),
                Link('cressy', One, _, edge='xeric', requires=None),
            ]),
            Edge('offeree', [
                Field('abila', String, _),
                Link('ferber', Many, _, edge='xeric', requires=None),
            ]),
        ]),
        """
        type xeric
          Record
            :derrida String

        type amb
          Record
            :loor String
            :cressy xeric

        type offeree
          Record
            :abila String
            :ferber
              List xeric
        """,
    )


def testDocs():
    assert_dumps(
        Graph([
            Edge('switzer', [
                Field('beatch', String, _, description="attribute beatch"),
            ], description="switzer description"),
            Edge('trine', [
                Field('propels', Optional[String], _,
                      description="attribute propels"),
                Link('cardura', One, _, edge='switzer', requires=None,
                     description="link cardura to switzer"),
            ], description="trine description"),
            Edge('packrat', [
                Field('pikes', String, _, description="attribute pikes"),
                Link('albus', Many, _, edge='switzer', requires=None,
                     description="link albus to switzer"),
            ], description="packrat description"),
        ]),
        """
        type switzer  ; switzer description
          Record
            :beatch String  ; attribute beatch

        type trine  ; trine description
          Record
            :propels  ; attribute propels
              Option String
            :cardura switzer  ; link cardura to switzer

        type packrat  ; packrat description
          Record
            :pikes String  ; attribute pikes
            :albus  ; link albus to switzer
              List switzer
        """,
    )

from __future__ import annotations

import pytest

from app.analyzer.type_parser import TypeParseError, TypeRef, parse_type, raw_name


def test_simple_generic_and_array() -> None:
    assert parse_type("int") == TypeRef("int")
    assert parse_type("String[][]") == TypeRef("String", dims=2)
    ref = parse_type("Map<String, List<Visit>>")
    assert ref.name == "Map" and [a.name for a in ref.args] == ["String", "List"]
    assert ref.args[1].args[0].name == "Visit"
    assert parse_type("List<String>[]").dims == 1


def test_qualified_nested_and_diamond() -> None:
    assert parse_type("java.util.List<Foo>").name == "java.util.List"
    assert parse_type("Map.Entry<String,Integer>").name == "Map.Entry"
    assert parse_type("ArrayList<>").args == ()
    assert parse_type("Outer<A>.Inner<B>").name == "Outer.Inner"


def test_wildcards_unions_and_annotations() -> None:
    ext = parse_type("? extends Foo")
    assert (ext.name, ext.wildcard_bound, ext.args[0].name) == ("?", "extends", "Foo")
    assert parse_type("? super Integer").wildcard_bound == "super"
    assert parse_type("?").args == ()
    union = parse_type("IllegalStateException|IllegalArgumentException")
    assert [a.name for a in union.alternatives] == [
        "IllegalStateException",
        "IllegalArgumentException",
    ]
    assert parse_type("A & B").alternatives[1].name == "B"
    assert parse_type("@NonNull String").name == "String"


def test_raw_name_strips_arguments_and_dims() -> None:
    assert raw_name("List<Map<String, Integer>>[]") == "List"


@pytest.mark.parametrize("bad", ["", "List<", "int[", "<>", "List<A B>"])
def test_unparseable_types_raise(bad: str) -> None:
    with pytest.raises(TypeParseError):
        parse_type(bad)

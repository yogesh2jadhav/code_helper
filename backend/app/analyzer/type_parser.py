"""Parse the type strings JavaParser emits (`Map<String, List<Foo>>`, `int[]`, `A|B`, ...)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TypeRef:
    name: str  # as written: "List", "java.util.List", "Outer.Inner", "?", "int", "|" for unions
    args: tuple[TypeRef, ...] = ()
    dims: int = 0
    wildcard_bound: str | None = None  # "extends" | "super" for `? extends X`; the bound is args[0]
    alternatives: tuple[TypeRef, ...] = ()  # members of a union (A|B) or intersection (A&B)


class TypeParseError(ValueError):
    pass


def parse_type(text: str) -> TypeRef:
    """Parse a type string. Raises TypeParseError on anything unrecognisable."""
    parser = _Parser(text.strip())
    ref = parser.parse_union()
    if parser.pos != len(parser.text):
        raise TypeParseError(f"trailing characters in type {text!r}")
    return ref


def raw_name(text: str) -> str:
    """The type with generic arguments and array brackets removed (`List<A>[]` -> `List`)."""
    return parse_type(text).name


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        self._skip_ws()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def parse_union(self) -> TypeRef:
        first = self.parse_single()
        members = [first]
        separator = ""
        while self._peek() in ("|", "&"):
            separator = self._peek()
            self.pos += 1
            members.append(self.parse_single())
        if len(members) == 1:
            return first
        return TypeRef(name=separator, alternatives=tuple(members))

    def parse_single(self) -> TypeRef:
        self._skip_ws()
        while self._peek() == "@":  # annotation on a type: skip `@Name` and `(...)`
            self._skip_annotation()
        if self._peek() == "?":
            return self._wildcard()
        name = self._qualified_name()
        args: tuple[TypeRef, ...] = ()
        if self._peek() == "<":
            args = self._type_args()
            # `Outer<A>.Inner<B>`: keep the innermost arguments, extend the name
            while self.pos < len(self.text) and self.text[self.pos] == ".":
                self.pos += 1
                name = f"{name}.{self._qualified_name()}"
                if self._peek() == "<":
                    args = self._type_args()
        dims = 0
        while self._peek() == "[":
            self.pos += 1
            if self._peek() != "]":
                raise TypeParseError(f"bad array type {self.text!r}")
            self.pos += 1
            dims += 1
        return TypeRef(name=name, args=args, dims=dims)

    def _wildcard(self) -> TypeRef:
        self.pos += 1  # '?'
        for keyword in ("extends", "super"):
            if self._peek() and self.text.startswith(keyword, self.pos):
                self.pos += len(keyword)
                return TypeRef(name="?", args=(self.parse_single(),), wildcard_bound=keyword)
        return TypeRef(name="?")

    def _qualified_name(self) -> str:
        self._skip_ws()
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] in "_$."
        ):
            self.pos += 1
        name = self.text[start : self.pos].rstrip(".")
        if not name:
            raise TypeParseError(f"expected a type name in {self.text!r} at {start}")
        return name

    def _type_args(self) -> tuple[TypeRef, ...]:
        self.pos += 1  # '<'
        args: list[TypeRef] = []
        if self._peek() == ">":  # diamond
            self.pos += 1
            return ()
        while True:
            args.append(self.parse_union())
            token = self._peek()
            self.pos += 1
            if token == ">":
                return tuple(args)
            if token != ",":
                raise TypeParseError(f"bad type arguments in {self.text!r}")

    def _skip_annotation(self) -> None:
        self.pos += 1
        self._qualified_name()
        if self._peek() == "(":
            depth = 0
            while self.pos < len(self.text):
                depth += {"(": 1, ")": -1}.get(self.text[self.pos], 0)
                self.pos += 1
                if depth == 0:
                    break

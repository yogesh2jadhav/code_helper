"""Project-wide symbol table built from parsed files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.analyzer.ast_models import Method, ParsedFile, TypeDecl
from app.analyzer.type_parser import TypeParseError, raw_name


@dataclass(frozen=True)
class AnalyzedFile:
    """A successfully parsed file plus the identity the rest of the system uses for it."""

    file_id: str
    path: str  # relative to the repository root where known
    parsed: ParsedFile


@dataclass(eq=False)
class FileInfo:
    file_id: str
    path: str
    package: str | None
    single_imports: dict[str, str] = field(default_factory=dict)  # simple name -> fqn
    wildcard_imports: list[str] = field(default_factory=list)  # package or enclosing-type prefix
    static_single: dict[str, list[str]] = field(default_factory=dict)  # member -> owner fqns
    static_wildcard: list[str] = field(default_factory=list)  # owner fqns


@dataclass(eq=False)
class FieldSymbol:
    id: str
    name: str
    type_text: str
    is_static: bool
    visibility: str
    line: int
    owner: ClassSymbol
    kind: str = "field"  # field | enum_constant | record_component


@dataclass(eq=False)
class MethodSymbol:
    id: str  # "<owner fqn>#<signature>"
    name: str
    signature: str
    kind: str  # method | constructor
    owner: ClassSymbol
    param_types: list[str]
    param_names: list[str]
    var_args: bool
    return_type: str | None
    type_params: list[str]
    is_static: bool
    is_abstract: bool
    visibility: str
    start_line: int
    end_line: int
    implicit: bool = False


@dataclass(eq=False)
class ClassSymbol:
    fqn: str
    name: str
    kind: str  # class | interface | enum | record | annotation
    package: str | None
    file: FileInfo
    start_line: int
    end_line: int
    outer: ClassSymbol | None
    nested: dict[str, str] = field(default_factory=dict)  # simple name -> fqn
    superclass: str | None = None
    interfaces: list[str] = field(default_factory=list)
    type_params: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    fields: dict[str, FieldSymbol] = field(default_factory=dict)
    methods: dict[str, list[MethodSymbol]] = field(default_factory=dict)
    constructors: list[MethodSymbol] = field(default_factory=list)

    @property
    def location(self) -> str:
        return f"{self.file.path}:{self.start_line}"


def type_param_names(params: Iterable[str]) -> list[str]:
    """`["T extends Comparable<T>", "R"]` -> `["T", "R"]`."""
    return [p.split()[0] for p in params if p.strip()]


class SymbolTable:
    def __init__(self, files: Iterable[AnalyzedFile]) -> None:
        self.classes: dict[str, list[ClassSymbol]] = {}
        self.packages: set[str] = set()
        self.files: list[FileInfo] = []
        self.file_classes: dict[str, list[ClassSymbol]] = {}
        self._declarations: dict[int, TypeDecl] = {}
        for analyzed in files:
            if analyzed.parsed.ok:
                self._add_file(analyzed)

    def lookup(self, fqn: str) -> list[ClassSymbol]:
        return self.classes.get(fqn, [])

    def has_package_prefix(self, dotted: str) -> bool:
        """True if `dotted` equals a project package or is a parent of one."""
        return any(p == dotted or p.startswith(dotted + ".") for p in self.packages)

    def declaration(self, cls: ClassSymbol) -> TypeDecl:
        return self._declarations[id(cls)]

    # ---- building ----------------------------------------------------------------------------

    def _add_file(self, analyzed: AnalyzedFile) -> None:
        parsed = analyzed.parsed
        info = FileInfo(analyzed.file_id, analyzed.path, parsed.package_name)
        for imp in parsed.imports:
            if imp.is_static:
                if imp.is_asterisk:
                    info.static_wildcard.append(imp.name)
                else:
                    owner, _, member = imp.name.rpartition(".")
                    info.static_single.setdefault(member, []).append(owner)
            elif imp.is_asterisk:
                info.wildcard_imports.append(imp.name)
            else:
                info.single_imports[imp.name.rpartition(".")[2]] = imp.name
        self.files.append(info)
        if parsed.package_name:
            self.packages.add(parsed.package_name)
        for decl in parsed.types:
            self._add_type(decl, info, None)

    def _add_type(self, decl: TypeDecl, info: FileInfo, outer: ClassSymbol | None) -> None:
        cls = ClassSymbol(
            fqn=decl.qualified_name,
            name=decl.name,
            kind=decl.kind,
            package=info.package,
            file=info,
            start_line=decl.start_line,
            end_line=decl.end_line,
            outer=outer,
            superclass=decl.superclass,
            interfaces=list(decl.interfaces),
            type_params=type_param_names(decl.type_parameters),
            modifiers=list(decl.modifiers),
            annotations=[a.name for a in decl.annotations],
            nested={n.name: n.qualified_name for n in decl.nested_types},
        )
        self.classes.setdefault(cls.fqn, []).append(cls)
        self.file_classes.setdefault(info.file_id, []).append(cls)
        self._declarations[id(cls)] = decl

        for f in decl.fields:
            cls.fields[f.name] = FieldSymbol(
                f"{cls.fqn}#{f.name}",
                f.name,
                f.type,
                "static" in f.modifiers,
                f.visibility,
                f.start_line,
                cls,
            )
        for constant in decl.enum_constants:
            cls.fields[constant] = FieldSymbol(
                f"{cls.fqn}#{constant}",
                constant,
                cls.fqn,
                True,
                "public",
                decl.start_line,
                cls,
                kind="enum_constant",
            )
        for component in decl.record_components:
            cls.fields[component.name] = FieldSymbol(
                f"{cls.fqn}#{component.name}",
                component.name,
                component.type,
                False,
                "private",
                decl.start_line,
                cls,
                kind="record_component",
            )

        for m in decl.methods:
            cls.methods.setdefault(m.name, []).append(self._method(cls, m))
        for c in decl.constructors:
            cls.constructors.append(self._method(cls, c))
        self._add_implicit_members(cls, decl)

        for nested in decl.nested_types:
            self._add_type(nested, info, cls)

    def _method(self, cls: ClassSymbol, m: Method) -> MethodSymbol:
        return MethodSymbol(
            id=f"{cls.fqn}#{m.signature}",
            name=m.name,
            signature=m.signature,
            kind="constructor" if m.kind != "method" else "method",
            owner=cls,
            param_types=[p.type for p in m.parameters],
            param_names=[p.name for p in m.parameters],
            var_args=bool(m.parameters and m.parameters[-1].var_args),
            return_type=m.return_type,
            type_params=type_param_names(m.type_parameters),
            is_static=m.is_static,
            is_abstract=m.is_abstract,
            visibility=m.visibility,
            start_line=m.start_line,
            end_line=m.end_line,
        )

    def _implicit(
        self,
        cls: ClassSymbol,
        name: str,
        kind: str,
        params: list[tuple[str, str]],
        return_type: str | None,
        is_static: bool = False,
    ) -> MethodSymbol:
        types = [t for t, _ in params]
        signature = f"{name}({','.join(types)})"
        return MethodSymbol(
            id=f"{cls.fqn}#{signature}",
            name=name,
            signature=signature,
            kind=kind,
            owner=cls,
            param_types=types,
            param_names=[n for _, n in params],
            var_args=False,
            return_type=return_type,
            type_params=[],
            is_static=is_static,
            is_abstract=False,
            visibility="public",
            start_line=cls.start_line,
            end_line=cls.start_line,
            implicit=True,
        )

    def _add_implicit_members(self, cls: ClassSymbol, decl: TypeDecl) -> None:
        """Members the compiler provides: default/canonical constructors, record accessors, enum
        `values()`/`valueOf()`."""
        if cls.kind == "record":
            components = [(c.type, c.name) for c in decl.record_components]
            for type_text, name in components:
                if not any(not m.param_types for m in cls.methods.get(name, [])):
                    cls.methods.setdefault(name, []).append(
                        self._implicit(cls, name, "method", [], type_text)
                    )
            canonical = [t for t, _ in components]
            declared = {tuple(c.param_types) for c in cls.constructors}
            if tuple(canonical) not in declared:
                cls.constructors.append(
                    self._implicit(cls, cls.name, "constructor", components, None)
                )
        elif cls.kind == "enum":
            cls.methods.setdefault("values", []).append(
                self._implicit(cls, "values", "method", [], f"{cls.fqn}[]", is_static=True)
            )
            cls.methods.setdefault("valueOf", []).append(
                self._implicit(
                    cls, "valueOf", "method", [("String", "name")], cls.fqn, is_static=True
                )
            )
        if cls.kind in ("class", "enum") and not cls.constructors:
            cls.constructors.append(self._implicit(cls, cls.name, "constructor", [], None))


def safe_raw_name(text: str) -> str | None:
    try:
        return raw_name(text)
    except TypeParseError:
        return None

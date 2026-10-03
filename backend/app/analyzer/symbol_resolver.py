"""Symbol resolution over parsed Java files.

Resolves type names, identifier uses, field accesses, method calls, constructor calls and method
references to symbols in the project's symbol table, following Java's scoping rules as far as
syntax alone allows. Anything that cannot be determined is reported as `ambiguous` or `unresolved`
with a reason; the resolver never silently picks a plausible-looking target.

Known limits (each surfaces as a reason, not a guess): no JDK member signatures (external calls
resolve to their *type* only), no generic type-argument substitution, no flow typing, no members
of anonymous/local classes, no Lombok-generated members.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.analyzer.ast_models import Expression, Method, ValueHint
from app.analyzer.jdk_types import (
    BOXES,
    FINAL_VALUE_TYPES,
    JAVA_LANG,
    KNOWN_JDK_TYPES,
    KNOWN_STATIC_FIELD_TYPES,
    LOMBOK_ANNOTATIONS,
    OBJECT_METHODS,
    PRIMITIVES,
    WIDENING,
)
from app.analyzer.resolution_models import (
    ClassResolution,
    FieldTypeResolution,
    FileResolution,
    MethodResolution,
    Origin,
    Resolution,
    ResolutionStatus,
    ResolutionSummary,
    TypeResolution,
)
from app.analyzer.symbol_table import (
    AnalyzedFile,
    ClassSymbol,
    FieldSymbol,
    FileInfo,
    MethodSymbol,
    SymbolTable,
    type_param_names,
)
from app.analyzer.type_parser import TypeParseError, TypeRef, parse_type
from app.logging_setup import log_event

logger = logging.getLogger(__name__)

R = ResolutionStatus
RESOLVABLE_KINDS = frozenset(
    {
        "name_ref",
        "field_access",
        "method_call",
        "object_creation",
        "method_ref",
        "variable_declaration",
    }
)
_DECL_KINDS = {
    "lambda_param": "lambda_parameter",
    "catch_param": "catch_parameter",
    "pattern": "pattern_variable",
    "param": "parameter",
}
_ENUM_OBJECT_METHODS = frozenset({"name", "ordinal", "compareTo", "getDeclaringClass"})


@dataclass(frozen=True)
class TypeScope:
    """Where a type name is being resolved: its file, enclosing classes, and type variables."""

    file: FileInfo
    classes: tuple[ClassSymbol, ...]
    type_vars: frozenset[str]

    @property
    def key(self) -> tuple[int, tuple[int, ...], frozenset[str]]:
        return (id(self.file), tuple(id(c) for c in self.classes), self.type_vars)


@dataclass
class Hierarchy:
    classes: list[ClassSymbol]  # the class itself, then its project supertypes
    external: list[str]  # supertypes outside the project (java.lang.Object not listed)
    unresolved: bool  # some supertype could not be resolved


@dataclass
class _Local:
    name: str
    type_text: str | None
    start: int
    end: int
    tags: list[str]
    initializer: ValueHint | None
    expr_id: int


@dataclass
class _MethodCtx:
    cls: ClassSymbol
    method_id: str
    method: Method
    scope: TypeScope
    exprs: dict[int, Expression]
    locals: list[_Local]
    params: dict[str, str]  # name -> declared type text (varargs already array-typed)
    receivers_of_field_access: set[int]
    results: dict[int, Resolution] = field(default_factory=dict)
    local_types: dict[int, TypeResolution | None] = field(default_factory=dict)


@dataclass
class _Var:
    kind: str
    status: R
    origin: Origin | None
    name: str
    value_type: TypeResolution | None
    symbol_id: str | None = None
    owner: str | None = None
    file: str | None = None
    line: int | None = None
    candidates: list[str] = field(default_factory=list)
    reason: str | None = None


def erasure(text: str) -> str:
    try:
        ref = parse_type(text)
    except TypeParseError:
        return text
    return ref.name + "[]" * ref.dims


class SymbolResolver:
    def __init__(self, table: SymbolTable) -> None:
        self.table = table
        self._type_cache: dict[tuple[object, str], TypeResolution] = {}
        self._hierarchy_cache: dict[int, Hierarchy] = {}
        self._hierarchy_in_progress: set[int] = set()
        self._param_cache: dict[int, list[TypeResolution]] = {}

    # ==== public API ==============================================================================

    def resolve_all(self, files: Iterable[AnalyzedFile]) -> list[FileResolution]:
        started = time.monotonic()
        results = [self.resolve_file(f) for f in files if f.parsed.ok]
        log_event(
            logger,
            "symbol_resolution_completed",
            files=len(results),
            classes=sum(len(r.classes) for r in results),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return results

    def resolve_file(self, analyzed: AnalyzedFile) -> FileResolution:
        out = FileResolution(file_id=analyzed.file_id, path=analyzed.path)
        for cls in self.table.file_classes.get(analyzed.file_id, []):
            out.classes.append(self._resolve_class(cls))
        return out

    @staticmethod
    def summarize(resolutions: Iterable[FileResolution]) -> ResolutionSummary:
        by_kind: dict[str, dict[str, int]] = {}
        calls = {"project": 0, "external": 0, "ambiguous": 0, "unresolved": 0}
        unresolved: dict[str, int] = {}
        ambiguous: dict[str, int] = {}
        files = classes = methods = refs = 0
        for fr in resolutions:
            files += 1
            for cr in fr.classes:
                classes += 1
                for mr in cr.methods:
                    methods += 1
                    for ref in mr.refs:
                        refs += 1
                        bucket = by_kind.setdefault(ref.kind, {})
                        bucket[ref.status.value] = bucket.get(ref.status.value, 0) + 1
                        reason = ref.reason or "unspecified"
                        if ref.status is R.UNRESOLVED:
                            unresolved[reason] = unresolved.get(reason, 0) + 1
                        elif ref.status is R.AMBIGUOUS:
                            ambiguous[reason] = ambiguous.get(reason, 0) + 1
                        if ref.kind == "method":
                            if ref.status is R.RESOLVED:
                                calls[
                                    "project" if ref.origin is Origin.PROJECT else "external"
                                ] += 1
                            else:
                                calls[ref.status.value] += 1
        return ResolutionSummary(
            files=files,
            classes=classes,
            methods=methods,
            references=refs,
            by_kind=by_kind,
            calls=calls,
            unresolved_reasons=dict(sorted(unresolved.items(), key=lambda kv: -kv[1])),
            ambiguous_reasons=dict(sorted(ambiguous.items(), key=lambda kv: -kv[1])),
        )

    # ==== class / method level ===============================================================

    def _resolve_class(self, cls: ClassSymbol) -> ClassResolution:
        decl = self.table.declaration(cls)
        scope = self.class_scope(cls)
        header = self.class_scope(cls, header=True)
        out = ClassResolution(fqn=cls.fqn, kind=cls.kind)
        if cls.superclass:
            out.superclass = self.resolve_type(cls.superclass, header)
        out.interfaces = [self.resolve_type(t, header) for t in cls.interfaces]
        for f in decl.fields:
            out.fields.append(
                FieldTypeResolution(name=f.name, type=self.resolve_type(f.type, scope))
            )
        for comp in decl.record_components:
            out.fields.append(
                FieldTypeResolution(name=comp.name, type=self.resolve_type(comp.type, scope))
            )

        symbols = {m.id: m for ms in cls.methods.values() for m in ms if not m.implicit}
        symbols.update({c.id: c for c in cls.constructors if not c.implicit})
        for method in [*decl.constructors, *decl.methods]:
            symbol = symbols.get(f"{cls.fqn}#{method.signature}")
            if symbol is not None:
                out.methods.append(self._resolve_method(cls, symbol, method))
        return out

    def _resolve_method(
        self, cls: ClassSymbol, symbol: MethodSymbol, method: Method
    ) -> MethodResolution:
        scope = self.class_scope(cls, symbol.type_params)
        params: list[TypeResolution] = []
        for p in method.parameters:
            t = self.resolve_type(p.type, scope)
            params.append(t.model_copy(update={"dims": t.dims + 1}) if p.var_args else t)
        out = MethodResolution(
            class_fqn=cls.fqn,
            method_id=symbol.id,
            name=method.name,
            signature=method.signature,
            kind=method.kind,
            parameters=params,
            return_type=self.resolve_type(method.return_type, scope)
            if method.return_type
            else None,
            throws=[self.resolve_type(t, scope) for t in method.throws_types],
        )
        ctx = self._method_ctx(cls, symbol, method, scope)
        for ex in method.expressions:
            if ex.kind in RESOLVABLE_KINDS:
                self._resolve_expr(ex.id, ctx)
        out.refs = [ctx.results[ex.id] for ex in method.expressions if ex.id in ctx.results]
        return out

    def _method_ctx(
        self, cls: ClassSymbol, symbol: MethodSymbol, method: Method, scope: TypeScope
    ) -> _MethodCtx:
        exprs = {e.id: e for e in method.expressions}
        locals_ = [
            _Local(
                e.name or "",
                e.type,
                e.start_line,
                e.scope_end_line or method.end_line,
                e.tags,
                e.initializer,
                e.id,
            )
            for e in method.expressions
            if e.kind == "variable_declaration"
        ]
        param_types = {p.name: p.type + ("[]" if p.var_args else "") for p in method.parameters}
        receivers = {
            e.receiver_expr_id
            for e in method.expressions
            if e.kind == "field_access" and e.receiver_expr_id is not None
        }
        return _MethodCtx(cls, symbol.id, method, scope, exprs, locals_, param_types, receivers)

    # ==== scopes and hierarchy ====================================================================

    def class_scope(
        self,
        cls: ClassSymbol,
        method_type_params: Iterable[str] = (),
        header: bool = False,
    ) -> TypeScope:
        """Scope for names written inside `cls`.

        With header=True (the `extends`/`implements` clause) the class's own members are not in
        scope, only its type parameters and what surrounds it; this also keeps supertype lookup
        from needing the hierarchy it is in the middle of computing.
        """
        chain: list[ClassSymbol] = []
        c: ClassSymbol | None = cls.outer if header else cls
        while c is not None:
            chain.append(c)
            c = c.outer
        type_vars = {tp for k in [cls, *chain] for tp in k.type_params} | set(method_type_params)
        return TypeScope(cls.file, tuple(chain), frozenset(type_vars))

    def hierarchy(self, cls: ClassSymbol) -> Hierarchy:
        cached = self._hierarchy_cache.get(id(cls))
        if cached is not None:
            return cached
        if id(cls) in self._hierarchy_in_progress:  # cyclic or self-referential declarations
            return Hierarchy([cls], [], True)
        self._hierarchy_in_progress.add(id(cls))
        classes: list[ClassSymbol] = []
        external: list[str] = []
        unresolved = False
        seen: set[int] = set()

        def visit(c: ClassSymbol) -> None:
            nonlocal unresolved
            if id(c) in seen:
                return
            seen.add(id(c))
            classes.append(c)
            if c.kind == "enum":
                external.append("java.lang.Enum")
            elif c.kind == "record":
                external.append("java.lang.Record")
            scope = self.class_scope(c, header=True)
            for text in ([c.superclass] if c.superclass else []) + c.interfaces:
                t = self.resolve_type(text, scope)
                if t.status is R.RESOLVED and t.origin is Origin.PROJECT:
                    target = self._class_of(t)
                    if target is None:
                        unresolved = True
                    else:
                        visit(target)
                elif t.status is R.RESOLVED and t.origin is Origin.EXTERNAL:
                    if t.fqn != "java.lang.Object" and t.fqn:
                        external.append(t.fqn)
                else:
                    unresolved = True

        try:
            visit(cls)
        finally:
            self._hierarchy_in_progress.discard(id(cls))
        result = Hierarchy(classes, list(dict.fromkeys(external)), unresolved)
        self._hierarchy_cache[id(cls)] = result
        return result

    def _class_of(self, t: TypeResolution) -> ClassSymbol | None:
        """The unique project class a resolved type refers to (None if absent or duplicated)."""
        if t.status is not R.RESOLVED or t.origin is not Origin.PROJECT or t.dims or not t.fqn:
            return None
        found = self.table.lookup(t.fqn)
        return found[0] if len(found) == 1 else None

    def _member_type(self, cls: ClassSymbol, name: str) -> TypeResolution | None:
        for c in self.hierarchy(cls).classes:
            fqn = c.nested.get(name)
            if fqn:
                return self._project(name, fqn)
        return None

    # ==== type resolution =========================================================================

    def resolve_type(self, text: str, scope: TypeScope) -> TypeResolution:
        key = (scope.key, text)
        cached = self._type_cache.get(key)
        if cached is not None:
            return cached
        try:
            result = self._resolve_ref(parse_type(text), scope, text)
        except TypeParseError:
            result = TypeResolution(
                text=text, status=R.UNRESOLVED, kind="unknown", reason="unparseable_type"
            )
        self._type_cache[key] = result
        return result

    def _resolve_ref(
        self, ref: TypeRef, scope: TypeScope, text: str | None = None
    ) -> TypeResolution:
        label = text if text is not None else ref.name + "[]" * ref.dims
        if ref.alternatives:
            members = [self._resolve_ref(a, scope) for a in ref.alternatives]
            ok = all(m.status is R.RESOLVED for m in members)
            return TypeResolution(
                text=label,
                status=R.RESOLVED if ok else R.UNRESOLVED,
                kind="union",
                args=members,
                reason=None if ok else "union_member_unresolved",
            )
        if ref.name == "?":
            return TypeResolution(
                text=label,
                status=R.RESOLVED,
                kind="wildcard",
                args=[self._resolve_ref(a, scope) for a in ref.args],
            )
        if ref.name in PRIMITIVES:
            return TypeResolution(
                text=label,
                status=R.RESOLVED,
                kind="primitive",
                origin=Origin.PRIMITIVE,
                fqn=ref.name,
                dims=ref.dims,
            )
        base = self._class_name(ref.name, scope)
        return base.model_copy(
            update={
                "text": label,
                "dims": ref.dims,
                "args": [self._resolve_ref(a, scope) for a in ref.args],
            }
        )

    def _class_name(self, name: str, scope: TypeScope) -> TypeResolution:
        return self._qualified(name, scope) if "." in name else self._simple(name, scope)

    def _simple(self, name: str, scope: TypeScope) -> TypeResolution:
        # 1. type variables
        if name in scope.type_vars:
            return TypeResolution(
                text=name, status=R.RESOLVED, kind="type_variable", origin=Origin.TYPE_VARIABLE
            )
        # 2. the enclosing classes themselves and their (inherited) member types
        for cls in scope.classes:
            if cls.name == name:
                return self._project(name, cls.fqn)
            nested = self._member_type(cls, name)
            if nested is not None:
                return nested
        single = scope.file.single_imports
        wildcards = scope.file.wildcard_imports
        package = scope.file.package
        # 3. single-type imports
        if name in single:
            return self._by_fqn(name, single[name])
        # 4. same package (including the default package)
        same = f"{package}.{name}" if package else name
        if self.table.lookup(same):
            return self._project(name, same)
        # 5. on-demand imports, then java.lang
        project_hits = sorted(
            {f"{w}.{name}" for w in wildcards if self.table.lookup(f"{w}.{name}")}
        )
        if project_hits:
            if len(project_hits) == 1:
                return self._project(name, project_hits[0])
            return TypeResolution(
                text=name,
                status=R.AMBIGUOUS,
                reason="multiple_on_demand_imports",
                candidates=project_hits,
            )
        external_hits = {
            f"{w}.{name}" for w in wildcards if name in KNOWN_JDK_TYPES.get(w, frozenset())
        }
        if name in JAVA_LANG:
            external_hits.add(f"java.lang.{name}")
        if len(external_hits) == 1:
            return self._external(name, next(iter(external_hits)))
        if external_hits:
            return TypeResolution(
                text=name,
                status=R.AMBIGUOUS,
                reason="multiple_on_demand_imports",
                candidates=sorted(external_hits),
            )
        unknown = sorted(f"{w}.{name}" for w in wildcards if not self.table.has_package_prefix(w))
        if unknown:
            return TypeResolution(
                text=name,
                status=R.UNRESOLVED,
                kind="unknown",
                reason="external_unverifiable",
                candidates=unknown,
            )
        return TypeResolution(text=name, status=R.UNRESOLVED, kind="unknown", reason="unknown_type")

    def _qualified(self, name: str, scope: TypeScope) -> TypeResolution:
        parts = name.split(".")
        head = self._simple(parts[0], scope)
        if head.status is R.RESOLVED and head.kind == "class":
            return self._descend(name, head, parts[1:])
        if head.status is R.AMBIGUOUS:
            return head.model_copy(update={"text": name})
        for i in range(len(parts), 0, -1):  # written fully qualified: longest project prefix
            prefix = ".".join(parts[:i])
            if self.table.lookup(prefix):
                return self._descend(name, self._project(name, prefix), parts[i:])
        if any(p[:1].isupper() for p in parts[1:]):
            return self._external(name, name)  # explicitly qualified JDK/library type
        return TypeResolution(
            text=name, status=R.UNRESOLVED, kind="unknown", reason="unknown_qualified_name"
        )

    def _descend(self, text: str, base: TypeResolution, rest: list[str]) -> TypeResolution:
        current = base
        for part in rest:
            if current.status is not R.RESOLVED:
                break
            if current.origin is Origin.PROJECT:
                cls = self._class_of(current)
                nested = self._member_type(cls, part) if cls else None
                if nested is None:
                    return TypeResolution(
                        text=text, status=R.UNRESOLVED, kind="unknown", reason="unknown_nested_type"
                    )
                current = nested
            elif current.origin is Origin.EXTERNAL:
                current = self._external(text, f"{current.fqn}.{part}")
            else:
                return TypeResolution(
                    text=text, status=R.UNRESOLVED, kind="unknown", reason="unknown_nested_type"
                )
        return current.model_copy(update={"text": text})

    def _project(self, text: str, fqn: str) -> TypeResolution:
        found = self.table.lookup(fqn)
        if len(found) > 1:
            return TypeResolution(
                text=text,
                status=R.AMBIGUOUS,
                origin=Origin.PROJECT,
                fqn=fqn,
                reason="duplicate_class_definition",
                candidates=[c.location for c in found],
            )
        return TypeResolution(text=text, status=R.RESOLVED, origin=Origin.PROJECT, fqn=fqn)

    @staticmethod
    def _external(text: str, fqn: str) -> TypeResolution:
        return TypeResolution(text=text, status=R.RESOLVED, origin=Origin.EXTERNAL, fqn=fqn)

    def _by_fqn(self, text: str, fqn: str) -> TypeResolution:
        return self._project(text, fqn) if self.table.lookup(fqn) else self._external(text, fqn)

    # ==== member lookup ===========================================================================

    def _methods_named(self, cls: ClassSymbol, name: str) -> tuple[list[MethodSymbol], Hierarchy]:
        hierarchy = self.hierarchy(cls)
        found: list[MethodSymbol] = []
        seen: set[tuple[str, ...]] = set()
        for c in hierarchy.classes:
            for m in c.methods.get(name, []):
                signature = tuple(erasure(t) for t in m.param_types)
                if signature not in seen:  # a subclass override hides the supertype's method
                    seen.add(signature)
                    found.append(m)
        return found, hierarchy

    def _find_field(self, cls: ClassSymbol, name: str) -> tuple[FieldSymbol | None, Hierarchy]:
        hierarchy = self.hierarchy(cls)
        for c in hierarchy.classes:
            if name in c.fields:
                return c.fields[name], hierarchy
        return None, hierarchy

    def _has_lombok(self, hierarchy: Hierarchy) -> bool:
        return any(a in LOMBOK_ANNOTATIONS for c in hierarchy.classes for a in c.annotations)

    # ==== expressions =============================================================================

    def _resolve_expr(self, expr_id: int, m: _MethodCtx) -> Resolution:
        cached = m.results.get(expr_id)
        if cached is not None:
            return cached
        ex = m.exprs[expr_id]
        if ex.kind == "name_ref":
            result = self._res_name(ex, m)
        elif ex.kind == "field_access":
            result = self._res_field_access(ex, m)
        elif ex.kind == "method_call":
            result = self._res_call(ex, m)
        elif ex.kind == "object_creation":
            result = self._res_new(ex, m)
        elif ex.kind == "method_ref":
            result = self._res_method_ref(ex, m)
        else:
            result = self._res_decl(ex, m)
        m.results[expr_id] = result
        return result

    @staticmethod
    def _make(
        ex: Expression,
        kind: str,
        status: R,
        *,
        name: str | None = None,
        origin: Origin | None = None,
        owner_type: str | None = None,
        symbol_id: str | None = None,
        file: str | None = None,
        line: int | None = None,
        type_ref: TypeResolution | None = None,
        value_type: TypeResolution | None = None,
        candidates: list[str] | None = None,
        reason: str | None = None,
        implicit: bool = False,
    ) -> Resolution:
        return Resolution(
            expression_id=ex.id,
            kind=kind,
            status=status,
            site_line=ex.start_line,
            name=name or ex.name or "",
            origin=origin,
            owner_type=owner_type,
            symbol_id=symbol_id,
            file=file,
            line=line,
            type_ref=type_ref,
            value_type=value_type,
            candidates=candidates or [],
            reason=reason,
            implicit=implicit,
        )

    # ---- declarations ---------------------------------------------------------------------------

    def _res_decl(self, ex: Expression, m: _MethodCtx) -> Resolution:
        variable_kind = next((v for k, v in _DECL_KINDS.items() if k in ex.tags), "local_variable")
        kind = f"{variable_kind}_declaration"  # distinct from uses (name_ref) of the variable
        local = next(x for x in m.locals if x.expr_id == ex.id)
        value = self._local_type(local, m)
        if value is not None:
            status, reason = value.status, value.reason
        elif ex.type is None or ex.type == "var":
            status, reason = R.UNRESOLVED, "inferred_type"
        else:  # pragma: no cover - resolve_type always returns a value
            status, reason = R.UNRESOLVED, "unknown_type"
        return self._make(
            ex,
            kind,
            status,
            origin=Origin.LOCAL,
            file=m.cls.file.path,
            line=ex.start_line,
            symbol_id=f"{m.method_id}${ex.name}@{ex.start_line}",
            value_type=value,
            candidates=value.candidates if value else None,
            reason=reason,
        )

    def _local_type(self, local: _Local, m: _MethodCtx) -> TypeResolution | None:
        if local.expr_id in m.local_types:
            return m.local_types[local.expr_id]
        m.local_types[local.expr_id] = None  # guards against self-referential initializers
        result: TypeResolution | None = None
        if local.type_text and local.type_text != "var":
            result = self.resolve_type(local.type_text, m.scope)
        elif local.type_text == "var" and local.initializer is not None:
            result = self._hint_type(local.initializer, local.start, m)
        m.local_types[local.expr_id] = result
        return result

    # ---- variables and names ----------------------------------------------------------------

    def _bind_variable(self, name: str, line: int, m: _MethodCtx, in_local: bool) -> _Var | None:
        """Locals, parameters, fields (own, inherited, enclosing), static imports. Not types."""
        best: _Local | None = None
        for d in m.locals:
            if (
                d.name == name
                and d.start <= line <= d.end
                and (best is None or d.start >= best.start)
            ):
                best = d
        if best is not None:
            kind = next((v for k, v in _DECL_KINDS.items() if k in best.tags), "local_variable")
            return _Var(
                kind,
                R.RESOLVED,
                Origin.LOCAL,
                name,
                self._local_type(best, m),
                symbol_id=f"{m.method_id}${name}@{best.start}",
                file=m.cls.file.path,
                line=best.start,
            )
        if name in m.params:
            return _Var(
                "parameter",
                R.RESOLVED,
                Origin.LOCAL,
                name,
                self.resolve_type(m.params[name], m.scope),
                symbol_id=f"{m.method_id}${name}",
                file=m.cls.file.path,
                line=m.method.start_line,
            )

        maybe_inherited = False
        for cls in m.scope.classes:
            found, hierarchy = self._find_field(cls, name)
            if found is not None:
                var = self._field_var(found)
                if in_local:  # an anonymous/local class might declare its own member of that name
                    var.status = R.AMBIGUOUS
                    var.reason = "possible_local_class_member"
                    var.candidates = [found.id]
                return var
            maybe_inherited = maybe_inherited or bool(hierarchy.external) or hierarchy.unresolved

        info = m.cls.file
        for owner in info.static_single.get(name, []):
            return self._static_import_var(name, owner)
        project_hits = [
            f
            for owner in info.static_wildcard
            for c in self.table.lookup(owner)
            for f in [c.fields.get(name)]
            if f is not None and f.is_static
        ]
        if len(project_hits) == 1:
            return self._field_var(project_hits[0])
        if len(project_hits) > 1:
            return _Var(
                "static_import",
                R.AMBIGUOUS,
                Origin.PROJECT,
                name,
                None,
                candidates=[f.id for f in project_hits],
                reason="multiple_static_imports",
            )
        if maybe_inherited:
            lombok = any(self._has_lombok(self.hierarchy(c)) for c in m.scope.classes)
            return _Var(
                "field",
                R.UNRESOLVED,
                None,
                name,
                None,
                reason="possible_lombok_generated"
                if lombok
                else "possible_inherited_external_field",
            )
        return None

    def _field_var(self, f: FieldSymbol) -> _Var:
        scope = self.class_scope(f.owner)
        kind = "field" if f.kind == "field" else f.kind
        return _Var(
            kind,
            R.RESOLVED,
            Origin.PROJECT,
            f.name,
            self.resolve_type(f.type_text, scope),
            symbol_id=f.id,
            owner=f.owner.fqn,
            file=f.owner.file.path,
            line=f.line,
        )

    def _static_import_var(self, name: str, owner: str) -> _Var:
        classes = self.table.lookup(owner)
        if len(classes) == 1 and name in classes[0].fields:
            return self._field_var(classes[0].fields[name])
        origin = Origin.PROJECT if classes else Origin.EXTERNAL
        return _Var("static_import", R.RESOLVED, origin, name, None, owner=owner)

    def _res_name(self, ex: Expression, m: _MethodCtx) -> Resolution:
        name = ex.name or ""
        in_local = "in_local_class" in ex.tags
        var = self._bind_variable(name, ex.start_line, m, in_local)
        if var is not None:
            return self._make(
                ex,
                var.kind,
                var.status,
                origin=var.origin,
                owner_type=var.owner,
                symbol_id=var.symbol_id,
                file=var.file,
                line=var.line,
                value_type=var.value_type,
                candidates=var.candidates,
                reason=var.reason,
            )

        t = self.resolve_type(name, m.scope)
        if t.status is not R.UNRESOLVED or (name[:1].isupper() and t.candidates):
            return self._make(
                ex,
                "type",
                t.status,
                origin=t.origin,
                owner_type=t.fqn,
                type_ref=t,
                candidates=t.candidates,
                reason=t.reason,
            )
        if ex.id in m.receivers_of_field_access:
            return self._make(
                ex, "package_candidate", R.UNRESOLVED, reason="possible_package_prefix"
            )
        return self._make(ex, "name", R.UNRESOLVED, reason="unknown_name")

    # ---- field access -------------------------------------------------------------------------

    def _res_field_access(self, ex: Expression, m: _MethodCtx) -> Resolution:
        name = ex.name or ""
        kind = ex.receiver_kind
        if kind == "this":
            return self._field_of_class(ex, m.cls, m, own_only=True)
        if kind == "super":
            return self._field_of_class(ex, m.cls, m, own_only=False, skip_self=True)
        if kind == "cast" and ex.receiver_type:
            return self._field_of_type(ex, self.resolve_type(ex.receiver_type, m.scope), m)
        if kind == "expr" and ex.receiver_expr_id is not None:
            recv = self._resolve_expr(ex.receiver_expr_id, m)
            if recv.kind in ("package", "package_candidate"):
                return self._package_member(ex, recv, m)
            if recv.kind == "type" and recv.type_ref is not None:
                return self._field_of_type(ex, recv.type_ref, m, static=True)
            if recv.value_type is not None:
                return self._field_of_type(ex, recv.value_type, m)
            return self._make(ex, "field", R.UNRESOLVED, reason="receiver_type_unknown")
        return self._make(ex, "field", R.UNRESOLVED, name=name, reason="receiver_not_analyzed")

    def _package_member(self, ex: Expression, recv: Resolution, m: _MethodCtx) -> Resolution:
        dotted = f"{recv.name}.{ex.name}"
        if self.table.lookup(dotted):
            return self._upgrade_chain(ex, recv, m, self._project(dotted, dotted))
        if (ex.name or "")[:1].isupper():  # `java.util.List`: package segments then a type
            return self._upgrade_chain(ex, recv, m, self._external(dotted, dotted))
        origin = Origin.PROJECT if self.table.has_package_prefix(dotted) else None
        if origin is Origin.PROJECT:
            return self._make(ex, "package", R.RESOLVED, origin=origin, name=dotted)
        return self._make(
            ex, "package_candidate", R.UNRESOLVED, name=dotted, reason="possible_package_prefix"
        )

    def _upgrade_chain(
        self, ex: Expression, recv: Resolution, m: _MethodCtx, t: TypeResolution
    ) -> Resolution:
        """The chain ended in a real type, so every package segment before it was a package."""
        current: Expression | None = m.exprs[recv.expression_id]
        while current is not None:
            res = m.results.get(current.id)
            if res is None or res.kind not in ("package_candidate", "package"):
                break
            segment_origin = (
                Origin.PROJECT if self.table.has_package_prefix(res.name) else Origin.EXTERNAL
            )
            m.results[current.id] = res.model_copy(
                update={
                    "kind": "package",
                    "status": R.RESOLVED,
                    "origin": segment_origin,
                    "reason": None,
                }
            )
            nxt = current.receiver_expr_id if current.kind == "field_access" else None
            current = m.exprs.get(nxt) if nxt is not None else None
        return self._make(
            ex,
            "type",
            t.status,
            origin=t.origin,
            owner_type=t.fqn,
            type_ref=t,
            name=t.text,
            reason=t.reason,
            candidates=t.candidates,
        )

    def _field_of_type(
        self, ex: Expression, t: TypeResolution, m: _MethodCtx, static: bool = False
    ) -> Resolution:
        name = ex.name or ""
        if t.status is not R.RESOLVED:
            return self._make(
                ex, "field", t.status, reason="receiver_type_unresolved", candidates=t.candidates
            )
        if t.dims and name == "length":
            return self._make(
                ex, "external_member", R.RESOLVED, origin=Origin.EXTERNAL, owner_type="array"
            )
        if t.kind in ("type_variable", "union", "wildcard", "primitive") or t.dims:
            return self._make(ex, "field", R.UNRESOLVED, reason=f"{t.kind}_receiver")
        if t.origin is Origin.EXTERNAL:
            known = KNOWN_STATIC_FIELD_TYPES.get((t.fqn or "", name))
            return self._make(
                ex,
                "external_member",
                R.RESOLVED,
                origin=Origin.EXTERNAL,
                owner_type=t.fqn,
                value_type=self._external(known, known) if known else None,
            )
        cls = self._class_of(t)
        if cls is None:
            return self._make(ex, "field", R.UNRESOLVED, reason="receiver_class_unavailable")
        return self._field_of_class(ex, cls, m, own_only=False, static=static)

    def _field_of_class(
        self,
        ex: Expression,
        cls: ClassSymbol,
        m: _MethodCtx,
        own_only: bool,
        skip_self: bool = False,
        static: bool = False,
    ) -> Resolution:
        name = ex.name or ""
        hierarchy = self.hierarchy(cls)
        candidates = hierarchy.classes[1:] if skip_self else hierarchy.classes
        for c in candidates:
            f = c.fields.get(name)
            if f is not None:
                var = self._field_var(f)
                return self._make(
                    ex,
                    var.kind,
                    R.RESOLVED,
                    origin=Origin.PROJECT,
                    owner_type=var.owner,
                    symbol_id=var.symbol_id,
                    file=var.file,
                    line=var.line,
                    value_type=var.value_type,
                )
        if static and name in cls.nested:
            t = self._project(name, cls.nested[name])
            return self._make(ex, "type", t.status, origin=t.origin, owner_type=t.fqn, type_ref=t)
        if hierarchy.external or hierarchy.unresolved:
            return self._make(
                ex,
                "field",
                R.UNRESOLVED,
                reason="possible_inherited_external_field",
                candidates=hierarchy.external,
            )
        if self._has_lombok(hierarchy):
            return self._make(ex, "field", R.UNRESOLVED, reason="possible_lombok_generated")
        return self._make(ex, "field", R.UNRESOLVED, reason="no_such_field")

    # ---- method calls ---------------------------------------------------------------------------

    def _res_call(self, ex: Expression, m: _MethodCtx) -> Resolution:
        kind = ex.receiver_kind or "none"
        in_local = "in_local_class" in ex.tags
        if kind == "none":
            return self._call_unqualified(ex, m, in_local)
        if kind == "this":
            return self._call_in_class(ex, m.cls, m)
        if kind == "super":
            return self._call_in_class(ex, m.cls, m, skip_self=True)
        if kind == "literal":
            return self._call_on_type(ex, self._literal_type(ex.receiver_type or ""), m)
        if kind == "cast" and ex.receiver_type:
            return self._call_on_type(ex, self.resolve_type(ex.receiver_type, m.scope), m)
        if kind == "expr" and ex.receiver_expr_id is not None:
            recv = self._resolve_expr(ex.receiver_expr_id, m)
            if recv.kind == "type" and recv.type_ref is not None:
                return self._call_on_type(ex, recv.type_ref, m)
            if recv.value_type is not None:
                return self._call_on_type(ex, recv.value_type, m)
            if recv.kind in ("package", "package_candidate"):
                return self._make(ex, "method", R.UNRESOLVED, reason="package_not_callable")
            reason = "receiver_type_unknown"
            if recv.reason and not recv.reason.startswith("receiver_type_unknown"):
                reason = f"receiver_type_unknown:{recv.reason}"
            return self._make(ex, "method", R.UNRESOLVED, reason=reason)
        return self._make(ex, "method", R.UNRESOLVED, reason="receiver_not_analyzed")

    def _literal_type(self, name: str) -> TypeResolution:
        if name in PRIMITIVES:
            return TypeResolution(
                text=name, status=R.RESOLVED, kind="primitive", origin=Origin.PRIMITIVE, fqn=name
            )
        return self._external(name, f"java.lang.{name}")

    def _call_on_type(self, ex: Expression, t: TypeResolution, m: _MethodCtx) -> Resolution:
        if t.status is R.AMBIGUOUS:
            return self._make(
                ex, "method", R.AMBIGUOUS, reason="receiver_type_ambiguous", candidates=t.candidates
            )
        if t.status is not R.RESOLVED:
            return self._make(
                ex,
                "method",
                R.UNRESOLVED,
                reason=f"receiver_type_unresolved:{t.reason}"
                if t.reason
                else "receiver_type_unresolved",
                candidates=t.candidates,
            )
        if t.dims:
            return self._make(ex, "method", R.RESOLVED, origin=Origin.EXTERNAL, owner_type="array")
        if t.kind in ("type_variable", "union", "wildcard", "primitive", "null"):
            return self._make(ex, "method", R.UNRESOLVED, reason=f"{t.kind}_receiver")
        if t.origin is Origin.EXTERNAL:
            return self._make(ex, "method", R.RESOLVED, origin=Origin.EXTERNAL, owner_type=t.fqn)
        cls = self._class_of(t)
        if cls is None:
            return self._make(ex, "method", R.UNRESOLVED, reason="receiver_class_unavailable")
        return self._call_in_class(ex, cls, m)

    def _call_in_class(
        self, ex: Expression, cls: ClassSymbol, m: _MethodCtx, skip_self: bool = False
    ) -> Resolution:
        methods, hierarchy = self._methods_named(cls, ex.name or "")
        if skip_self:
            methods = [x for x in methods if x.owner is not cls]
        return self._finish_call(ex, methods, [hierarchy], m)

    def _call_unqualified(self, ex: Expression, m: _MethodCtx, in_local: bool) -> Resolution:
        name = ex.name or ""
        hierarchies: list[Hierarchy] = []
        for cls in m.scope.classes:  # innermost class that has a method of that name wins
            methods, hierarchy = self._methods_named(cls, name)
            hierarchies.append(hierarchy)
            if methods:
                result = self._finish_call(ex, methods, hierarchies, m)
                if in_local and result.status is R.RESOLVED:
                    return result.model_copy(
                        update={
                            "status": R.AMBIGUOUS,
                            "reason": "possible_local_class_member",
                            "candidates": [result.symbol_id] if result.symbol_id else [],
                        }
                    )
                return result

        info = m.cls.file
        owners = info.static_single.get(name, [])
        if owners:
            return self._static_imported_call(ex, owners, m)
        wildcard_hits = [
            (owner, meths)
            for owner in info.static_wildcard
            for c in self.table.lookup(owner)
            for meths in [self._methods_named(c, name)[0]]
            if meths
        ]
        if wildcard_hits:
            return self._finish_call(ex, [x for _, ms in wildcard_hits for x in ms], hierarchies, m)

        if name in OBJECT_METHODS:
            return self._make(
                ex, "method", R.RESOLVED, origin=Origin.EXTERNAL, owner_type="java.lang.Object"
            )
        if in_local:
            return self._make(ex, "method", R.UNRESOLVED, reason="possible_local_class_member")
        external_wildcards = [o for o in info.static_wildcard if not self.table.lookup(o)]
        if external_wildcards:
            return self._make(
                ex,
                "method",
                R.UNRESOLVED,
                reason="static_wildcard_external",
                candidates=external_wildcards,
            )
        return self._fallback_unresolved(ex, hierarchies)

    def _static_imported_call(self, ex: Expression, owners: list[str], m: _MethodCtx) -> Resolution:
        project = [c for o in owners for c in self.table.lookup(o)]
        if project:
            methods = [
                x for c in project for x in self._methods_named(c, ex.name or "")[0] if x.is_static
            ]
            if methods:
                return self._finish_call(ex, methods, [], m)
        if len(owners) == 1 and not self.table.lookup(owners[0]):
            return self._make(
                ex, "method", R.RESOLVED, origin=Origin.EXTERNAL, owner_type=owners[0]
            )
        return self._make(
            ex, "method", R.AMBIGUOUS, reason="multiple_static_imports", candidates=owners
        )

    def _finish_call(
        self,
        ex: Expression,
        methods: list[MethodSymbol],
        hierarchies: list[Hierarchy],
        m: _MethodCtx,
    ) -> Resolution:
        argc = ex.arg_count or 0
        applicable = [x for x in methods if _arity_ok(x, argc)]
        if not applicable:
            if methods:
                return self._make(
                    ex,
                    "method",
                    R.UNRESOLVED,
                    reason="no_matching_arity",
                    candidates=[x.id for x in methods],
                )
            return self._fallback_unresolved(ex, hierarchies)
        chosen = self._narrow(applicable, ex, m)
        if len(chosen) == 1:
            return self._method_result(ex, chosen[0], R.RESOLVED)
        returns = [self._return_type(c) for c in chosen]
        same_return = len({r.fqn if r else None for r in returns}) == 1
        value_type = returns[0] if same_return else None
        return self._make(
            ex,
            "method",
            R.AMBIGUOUS,
            origin=Origin.PROJECT,
            reason="overload_not_disambiguated",
            candidates=[c.id for c in chosen],
            value_type=value_type,
        )

    def _fallback_unresolved(self, ex: Expression, hierarchies: list[Hierarchy]) -> Resolution:
        name = ex.name or ""
        if name in OBJECT_METHODS:
            return self._make(
                ex, "method", R.RESOLVED, origin=Origin.EXTERNAL, owner_type="java.lang.Object"
            )
        external = list(dict.fromkeys(e for h in hierarchies for e in h.external))
        if any(self._has_lombok(h) for h in hierarchies):
            return self._make(ex, "method", R.UNRESOLVED, reason="possible_lombok_generated")
        if external or any(h.unresolved for h in hierarchies):
            return self._make(
                ex,
                "method",
                R.UNRESOLVED,
                reason="possible_inherited_external_method",
                candidates=external,
            )
        return self._make(ex, "method", R.UNRESOLVED, reason="no_such_method")

    def _method_result(self, ex: Expression, symbol: MethodSymbol, status: R) -> Resolution:
        return self._make(
            ex,
            "method" if symbol.kind == "method" else "constructor",
            status,
            origin=Origin.PROJECT,
            owner_type=symbol.owner.fqn,
            symbol_id=symbol.id,
            file=symbol.owner.file.path,
            line=symbol.start_line,
            value_type=self._return_type(symbol),
            implicit=symbol.implicit,
        )

    def _return_type(self, symbol: MethodSymbol) -> TypeResolution | None:
        if symbol.return_type is None or symbol.return_type == "void":
            return None
        return self.resolve_type(
            symbol.return_type, self.class_scope(symbol.owner, symbol.type_params)
        )

    # ---- overload narrowing -------------------------------------------------------------------

    def param_types(self, symbol: MethodSymbol) -> list[TypeResolution]:
        cached = self._param_cache.get(id(symbol))
        if cached is None:
            scope = self.class_scope(symbol.owner, symbol.type_params)
            cached = [self.resolve_type(t, scope) for t in symbol.param_types]
            self._param_cache[id(symbol)] = cached
        return cached

    def _narrow(
        self, candidates: list[MethodSymbol], ex: Expression, m: _MethodCtx
    ) -> list[MethodSymbol]:
        if len(candidates) <= 1:
            return candidates
        hints = ex.args or []
        arg_types = [self._hint_type(h, ex.start_line, m) for h in hints]
        compatible = [
            c
            for c in candidates
            if all(
                self._maybe_assignable(a, p)
                for a, p in zip(arg_types, _params_for(self, c, len(hints)), strict=False)
            )
        ]
        if not compatible:
            compatible = candidates  # our assignability model may be wrong; stay honest, not clever
        fixed = [c for c in compatible if not c.var_args or len(c.param_types) == len(hints)]
        if fixed:
            compatible = fixed
        if (
            len(compatible) > 1
            and arg_types
            and all(
                a is not None and a.status is R.RESOLVED and a.kind != "null" for a in arg_types
            )
        ):
            exact = [
                c
                for c in compatible
                if all(
                    a is not None and p.fqn == a.fqn and p.dims == a.dims
                    for a, p in zip(arg_types, self.param_types(c), strict=False)
                )
            ]
            if len(exact) == 1:
                return exact
        return compatible

    def _hint_type(self, h: ValueHint, line: int, m: _MethodCtx) -> TypeResolution | None:
        if h.kind == "literal" and h.type:
            if h.type == "null":
                return TypeResolution(text="null", status=R.RESOLVED, kind="null")
            if h.type == "Class":
                return self._external("Class", "java.lang.Class")
            return self._literal_type(h.type)
        if h.kind in ("new", "cast") and h.type:
            return self.resolve_type(h.type, m.scope)
        if h.kind == "name" and h.name:
            var = self._bind_variable(h.name, line, m, False)
            return var.value_type if var and var.status is R.RESOLVED else None
        if h.kind == "this":
            return self._project(m.cls.name, m.cls.fqn)
        if h.kind == "expr" and h.expr_id is not None:
            res = self._resolve_expr(h.expr_id, m)
            return res.type_ref if res.kind == "type" else res.value_type
        return None

    def _maybe_assignable(self, a: TypeResolution | None, p: TypeResolution) -> bool:
        """False only when `a` definitely cannot be passed as `p`; True when unsure."""
        if a is None or a.status is not R.RESOLVED or p.status is not R.RESOLVED:
            return True
        if p.kind in ("type_variable", "wildcard", "union") or a.kind in (
            "type_variable",
            "wildcard",
            "union",
        ):
            return True
        if a.kind == "null":
            return p.kind != "primitive" or p.dims > 0
        if p.fqn == "java.lang.Object" and p.dims == 0:
            return True
        if a.dims != p.dims:
            return False
        if a.fqn == p.fqn:
            return True

        a_prim = a.fqn if a.kind == "primitive" else _unboxed(a.fqn)
        p_prim = p.fqn if p.kind == "primitive" else _unboxed(p.fqn)
        if a_prim and p_prim:
            if a.kind == "primitive" and p.kind != "primitive":
                return False  # boxing only yields the matching wrapper (same fqn handled above)
            return a_prim == p_prim or p_prim in WIDENING.get(a_prim, set())
        if a_prim and (p.origin is Origin.PROJECT or p.fqn in FINAL_VALUE_TYPES):
            return False
        if p_prim and (a.origin is Origin.PROJECT or a.fqn in FINAL_VALUE_TYPES):
            return False

        a_cls, p_cls = self._class_of(a), self._class_of(p)
        if a_cls is not None:
            h = self.hierarchy(a_cls)
            if p_cls is not None:
                return p_cls in h.classes or h.unresolved
            return bool(h.external or h.unresolved)  # an external supertype might be `p`
        if p_cls is not None:
            return False  # an external type cannot extend a project class
        return not (a.fqn in FINAL_VALUE_TYPES and p.fqn in FINAL_VALUE_TYPES)

    # ---- constructors and method references ---------------------------------------------------

    def _res_new(self, ex: Expression, m: _MethodCtx) -> Resolution:
        t = self.resolve_type(ex.type or ex.name or "", m.scope)
        if t.status is not R.RESOLVED:
            return self._make(
                ex, "constructor", t.status, type_ref=t, candidates=t.candidates, reason=t.reason
            )
        if t.origin is Origin.EXTERNAL:
            return self._make(
                ex,
                "constructor",
                R.RESOLVED,
                origin=Origin.EXTERNAL,
                owner_type=t.fqn,
                value_type=t,
            )
        cls = self._class_of(t)
        if cls is None:
            return self._make(
                ex, "constructor", R.UNRESOLVED, reason="class_unavailable", value_type=t
            )
        if (
            cls.kind in ("interface", "annotation")
            or "abstract" in cls.modifiers
            and "anonymous_class" in ex.tags
        ):
            return self._make(
                ex,
                "constructor",
                R.RESOLVED,
                origin=Origin.PROJECT,
                owner_type=cls.fqn,
                file=cls.file.path,
                line=cls.start_line,
                value_type=t,
                reason="anonymous_implementation",
            )
        argc = ex.arg_count or 0
        applicable = [c for c in cls.constructors if _arity_ok(c, argc)]
        if not applicable:
            hierarchy = self.hierarchy(cls)
            reason = (
                "possible_lombok_generated"
                if self._has_lombok(hierarchy)
                else "no_matching_constructor"
            )
            return self._make(
                ex,
                "constructor",
                R.UNRESOLVED,
                value_type=t,
                reason=reason,
                candidates=[c.id for c in cls.constructors],
            )
        chosen = self._narrow(applicable, ex, m)
        if len(chosen) == 1:
            result = self._method_result(ex, chosen[0], R.RESOLVED)
            return result.model_copy(update={"value_type": t, "name": cls.name})
        return self._make(
            ex,
            "constructor",
            R.AMBIGUOUS,
            origin=Origin.PROJECT,
            value_type=t,
            reason="overload_not_disambiguated",
            candidates=[c.id for c in chosen],
        )

    def _res_method_ref(self, ex: Expression, m: _MethodCtx) -> Resolution:
        scope_text = (ex.scope or "").strip()
        ident = ex.name or ""
        target: TypeResolution | None = None
        if scope_text == "this":
            target = self._project(m.cls.name, m.cls.fqn)
        elif scope_text.isidentifier():
            var = self._bind_variable(scope_text, ex.start_line, m, "in_local_class" in ex.tags)
            if var is not None:
                if var.value_type is None or var.status is not R.RESOLVED:
                    return self._make(
                        ex, "method_ref", R.UNRESOLVED, reason="receiver_type_unknown"
                    )
                target = var.value_type
        if target is None:
            target = self.resolve_type(scope_text, m.scope)
        if target.status is not R.RESOLVED:
            return self._make(
                ex,
                "method_ref",
                target.status,
                reason="receiver_type_unresolved",
                candidates=target.candidates,
            )
        if target.dims or target.origin is Origin.EXTERNAL or target.kind != "class":
            if target.kind in ("type_variable", "union", "wildcard"):
                return self._make(ex, "method_ref", R.UNRESOLVED, reason=f"{target.kind}_receiver")
            return self._make(
                ex,
                "method_ref",
                R.RESOLVED,
                origin=Origin.EXTERNAL,
                owner_type=target.fqn or "array",
            )
        cls = self._class_of(target)
        if cls is None:
            return self._make(ex, "method_ref", R.UNRESOLVED, reason="receiver_class_unavailable")
        if ident == "new":
            symbols = cls.constructors
            hierarchies: list[Hierarchy] = []
        else:
            symbols, hierarchy = self._methods_named(cls, ident)
            hierarchies = [hierarchy]
        if not symbols:
            return self._fallback_unresolved(ex, hierarchies)
        if len(symbols) == 1:
            return self._method_result(ex, symbols[0], R.RESOLVED).model_copy(
                update={"kind": "method_ref"}
            )
        return self._make(
            ex,
            "method_ref",
            R.AMBIGUOUS,
            origin=Origin.PROJECT,
            reason="overloaded_method_reference",
            candidates=[s.id for s in symbols],
        )


def _arity_ok(m: MethodSymbol, argc: int) -> bool:
    n = len(m.param_types)
    return argc == n or (m.var_args and argc >= n - 1)


def _unboxed(fqn: str | None) -> str | None:
    if fqn and fqn.startswith("java.lang."):
        return BOXES.get(fqn[len("java.lang.") :])
    return None


def _params_for(resolver: SymbolResolver, m: MethodSymbol, argc: int) -> list[TypeResolution]:
    """Parameter type for each argument position, expanding a trailing varargs parameter."""
    params = resolver.param_types(m)
    if not m.var_args or not params:
        return params
    fixed, last = params[:-1], params[-1]
    element = last.model_copy(update={"dims": max(0, last.dims - 1)})
    # the call may pass the array itself when argc == len(params); accept either by being lenient
    if argc == len(params):
        return [*fixed, element.model_copy(update={"kind": "wildcard", "dims": 0})]
    return [*fixed, *([element] * max(0, argc - len(fixed)))]


__all__ = ["SymbolResolver", "type_param_names"]

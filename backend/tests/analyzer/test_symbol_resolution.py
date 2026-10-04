"""Phase 3: symbol table and resolution, run against the multi-file fixtures/project sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.analyzer import JavaParserAnalyzer
from app.analyzer.ast_models import Expression
from app.analyzer.resolution_models import (
    ClassResolution,
    MethodResolution,
    Origin,
    Resolution,
    ResolutionStatus,
    TypeResolution,
)
from app.analyzer.symbol_resolver import SymbolResolver
from app.analyzer.symbol_table import AnalyzedFile, SymbolTable
from tests.conftest import PROJECT

R = ResolutionStatus
PS = "com.acme.service.PricingService"


@dataclass
class Project:
    files: list[AnalyzedFile]
    table: SymbolTable
    resolver: SymbolResolver
    classes: dict[str, ClassResolution]
    exprs: dict[tuple[str, str], dict[int, Expression]]

    def method(self, fqn: str, signature: str) -> MethodResolution:
        return next(m for m in self.classes[fqn].methods if m.signature == signature)

    def refs(
        self,
        fqn: str,
        signature: str,
        *,
        kind: str | None = None,
        name: str | None = None,
        text: str | None = None,
    ) -> list[Resolution]:
        """References in a method. `text` matches the expression source exactly, or as a prefix
        when it ends with `*`."""
        exprs = self.exprs[(fqn, signature)]

        def text_ok(source: str) -> bool:
            if text is None:
                return True
            return source.startswith(text[:-1]) if text.endswith("*") else source == text

        return [
            r
            for r in self.method(fqn, signature).refs
            if (kind is None or r.kind == kind)
            and (name is None or r.name == name)
            and text_ok(exprs[r.expression_id].text)
        ]

    def one(self, fqn: str, signature: str, nth: int | None = None, **kw: str | None) -> Resolution:
        found = self.refs(fqn, signature, **kw)
        if nth is None:
            assert len(found) == 1, f"{signature} {kw}: {len(found)} matches"
            return found[0]
        return found[nth]

    def call(self, signature: str, text: str, fqn: str = PS, nth: int | None = None) -> Resolution:
        return self.one(fqn, signature, nth, kind="method", text=text)


def _types(ts: list) -> list:  # type: ignore[type-arg]
    out = []
    for t in ts:
        out.append(t)
        out.extend(_types(t.nested_types))
    return out


@pytest.fixture(scope="module")
def project(java_analyzer: JavaParserAnalyzer) -> Project:
    paths = sorted(PROJECT.rglob("*.java"))
    parsed = java_analyzer.analyze_files(paths)
    files = [
        AnalyzedFile(str(i), p.relative_to(PROJECT).as_posix(), r)
        for i, (p, r) in enumerate(zip(paths, parsed, strict=True))
    ]
    assert all(f.parsed.ok for f in files), [f.parsed.errors for f in files if not f.parsed.ok]
    table = SymbolTable(files)
    resolver = SymbolResolver(table)
    classes = {c.fqn: c for fr in resolver.resolve_all(files) for c in fr.classes}
    exprs = {
        (t.qualified_name, m.signature): {e.id: e for e in m.expressions}
        for f in files
        for t in _types(f.parsed.types)
        for m in [*t.methods, *t.constructors]
    }
    return Project(files, table, resolver, classes, exprs)


# ---- symbol table -----------------------------------------------------------------------------


def test_symbol_table_lists_classes_packages_and_imports(project: Project) -> None:
    table = project.table
    assert {"com.acme.model.Customer", "com.acme.service.PricingService.Helper"} <= set(
        table.classes
    )
    assert {"com.acme.model", "com.acme.service", "com.acme.util"} <= table.packages
    info = table.lookup(PS)[0].file
    assert info.single_imports["List"] == "java.util.List"
    assert info.static_single == {
        "trim": ["com.acme.util.Strings"],
        "counting": ["java.util.stream.Collectors"],
    }
    assert {"com.acme.model", "com.acme.a", "com.acme.b", "org.example.external"} == set(
        info.wildcard_imports
    )
    assert len(table.lookup("com.acme.dup.Thing")) == 2  # same FQN in main/ and test/


def test_compiler_provided_members_exist_and_are_marked_implicit(project: Project) -> None:
    money = project.table.lookup("com.acme.model.Money")[0]
    assert {m.signature for ms in money.methods.values() for m in ms if m.implicit} == {
        "cents()",
        "currency()",
    }
    (canonical,) = money.constructors
    assert (canonical.signature, canonical.implicit) == ("Money(long,String)", True)

    status = project.table.lookup("com.acme.model.Status")[0]
    assert {"values", "valueOf"} <= set(status.methods)
    assert status.fields["ACTIVE"].kind == "enum_constant"
    assert [c.signature for c in status.constructors] == ["Status()"]  # implicit default

    customer = project.table.lookup("com.acme.model.Customer")[0]
    assert [c.signature for c in customer.constructors] == ["Customer()", "Customer(String)"]
    assert not any(c.implicit for c in customer.constructors)  # declared ones suppress the default


def test_hierarchy_includes_project_supertypes_and_flags_external_ones(project: Project) -> None:
    r, t = project.resolver, project.table
    pricing = r.hierarchy(t.lookup(PS)[0])
    assert [c.name for c in pricing.classes] == ["PricingService", "BasePricing", "Auditable"]
    assert pricing.external == [] and not pricing.unresolved
    assert r.hierarchy(t.lookup("com.acme.model.Status")[0]).external == ["java.lang.Enum"]
    assert r.hierarchy(t.lookup("com.acme.model.Money")[0]).external == ["java.lang.Record"]


# ---- type references --------------------------------------------------------------------------


def test_class_level_type_references(project: Project) -> None:
    pricing = project.classes[PS]
    assert pricing.superclass is not None
    assert (pricing.superclass.status, pricing.superclass.origin, pricing.superclass.fqn) == (
        R.RESOLVED,
        Origin.PROJECT,
        "com.acme.service.BasePricing",
    )
    base = project.classes["com.acme.service.BasePricing"]
    assert [(i.fqn, i.origin) for i in base.interfaces] == [
        ("com.acme.model.Auditable", Origin.PROJECT)
    ]

    history = next(f.type for f in pricing.fields if f.name == "history")
    assert (history.fqn, history.origin) == ("java.util.List", Origin.EXTERNAL)  # single import
    assert [(a.fqn, a.origin) for a in history.args] == [("com.acme.model.Money", Origin.PROJECT)]
    owner = next(f.type for f in pricing.fields if f.name == "owner")
    assert owner.fqn == "com.acme.model.Customer"  # via `import com.acme.model.*`


def test_method_signature_types_and_type_variables(project: Project) -> None:
    mx = project.method(PS, "max(T,T)")
    assert [(p.kind, p.origin) for p in mx.parameters] == [
        ("type_variable", Origin.TYPE_VARIABLE)
    ] * 2
    assert mx.return_type is not None and mx.return_type.kind == "type_variable"
    price = project.method(PS, "price(Customer)")
    assert price.parameters[0].fqn == "com.acme.model.Customer"
    assert price.return_type is not None and price.return_type.kind == "primitive"


def test_resolve_type_scoping_rules(project: Project) -> None:
    r = project.resolver
    scope = r.class_scope(project.table.lookup(PS)[0])

    def res(text: str) -> TypeResolution:
        return r.resolve_type(text, scope)

    assert res("int[]").dims == 1 and res("int[]").origin is Origin.PRIMITIVE
    assert res("String").fqn == "java.lang.String"  # implicit java.lang
    assert res("com.acme.model.Customer").origin is Origin.PROJECT  # written fully qualified
    assert res("java.util.concurrent.TimeUnit").origin is Origin.EXTERNAL
    assert res("Helper").fqn == f"{PS}.Helper"  # member type of the enclosing class
    assert res(f"{PS}.Helper").fqn == f"{PS}.Helper"
    entry = res("java.util.Map.Entry<String, Money>")
    assert entry.fqn == "java.util.Map.Entry" and entry.args[1].fqn == "com.acme.model.Money"
    wildcard = res("List<? extends Money>").args[0]
    assert wildcard.kind == "wildcard" and wildcard.args[0].fqn == "com.acme.model.Money"
    union = res("Money|Customer")
    assert union.kind == "union" and union.status is R.RESOLVED
    assert res("Money|Nope").status is R.UNRESOLVED
    assert res("List<").reason == "unparseable_type"
    nope = res("Nope")
    assert nope.status is R.UNRESOLVED and nope.reason == "external_unverifiable"
    assert nope.candidates == ["org.example.external.Nope"]  # only the non-project wildcard


def test_types_in_the_default_package_and_same_package_need_no_import(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    (tmp_path / "a").mkdir()
    sources = {
        "a/One.java": "package p;\nclass One { Two two; Missing m; }\n",
        "a/Two.java": "package p;\nclass Two {}\n",
        "a/Top.java": "class Top { Bare b; }\n",
        "a/Bare.java": "class Bare {}\n",
    }
    paths = []
    for rel, text in sources.items():
        path = tmp_path / rel
        path.write_text(text)
        paths.append(path)
    files = [
        AnalyzedFile(str(i), p.name, r)
        for i, (p, r) in enumerate(zip(paths, java_analyzer.analyze_files(paths), strict=True))
    ]
    classes = {
        c.fqn: c for fr in SymbolResolver(SymbolTable(files)).resolve_all(files) for c in fr.classes
    }
    one = {f.name: f.type for f in classes["p.One"].fields}
    assert (one["two"].fqn, one["two"].origin) == ("p.Two", Origin.PROJECT)
    assert one["m"].status is R.UNRESOLVED and one["m"].reason == "unknown_type"
    assert classes["Top"].fields[0].type.fqn == "Bare"  # default package


def test_ambiguity_is_reported_not_resolved(project: Project) -> None:
    util = project.one(PS, "problems(Customer)", kind="type", name="Util")
    assert util.status is R.AMBIGUOUS and util.reason == "multiple_on_demand_imports"
    assert util.candidates == ["com.acme.a.Util", "com.acme.b.Util"]
    call = project.call("problems(Customer)", "Util.go()")
    assert call.status is R.AMBIGUOUS and call.reason == "receiver_type_ambiguous"

    thing = project.one(PS, "problems(Customer)", kind="type", name="Thing")
    assert thing.status is R.AMBIGUOUS and thing.reason == "duplicate_class_definition"
    assert sorted(thing.candidates) == [
        "main/com/acme/dup/Thing.java:3",
        "test/com/acme/dup/Thing.java:3",
    ]
    new_thing = project.one(PS, "problems(Customer)", kind="constructor", text="new Thing()")
    assert new_thing.status is R.AMBIGUOUS


# ---- identifiers ------------------------------------------------------------------------------


def test_parameters_locals_and_fields(project: Project) -> None:
    sig = "overloads(Customer)"
    c = project.refs(PS, sig, kind="parameter", name="c")
    assert c and all(
        r.origin is Origin.LOCAL
        and r.value_type is not None
        and r.value_type.fqn == "com.acme.model.Customer"
        for r in c
    )
    owner = project.one(PS, sig, kind="field", name="owner")
    source = (PROJECT / "com/acme/service/PricingService.java").read_text().split("\n")
    declared_at = next(i for i, ln in enumerate(source, 1) if "private final Customer owner;" in ln)
    assert (owner.symbol_id, owner.origin, owner.line) == (
        f"{PS}#owner",
        Origin.PROJECT,
        declared_at,
    )
    assert owner.file == "com/acme/service/PricingService.java"

    inherited = project.one(PS, "inherited()", kind="field", name="rate", text="rate")
    assert inherited.symbol_id == "com.acme.service.BasePricing#rate"  # field of the superclass


def test_parameter_and_local_shadow_fields(project: Project) -> None:
    scoping = "scoping(Customer)"
    # first `owner.getName()` is in the method body; the second is a capture inside the anonymous class
    for nth in (0, 1):
        call = project.call(scoping, "owner.getName()", nth=nth)
        assert call.symbol_id == "com.acme.model.Customer#getName()"
    param = project.refs(PS, scoping, kind="parameter", name="owner")
    assert len(param) >= 2  # `owner.getName()` and the capture inside the anonymous class
    local = project.one(PS, scoping, kind="local_variable", name="history")
    assert local.origin is Origin.LOCAL and local.value_type is not None
    assert local.value_type.fqn == "int"
    for nth in (0, 1):  # `this.history.clear()` and `items = this.history` stay the field
        field = project.one(PS, scoping, nth, kind="field", name="history")
        assert field.symbol_id == f"{PS}#history"


def test_lambda_parameter_shadows_inherited_field(project: Project) -> None:
    sig = "shadowing()"
    lam = project.one(PS, sig, kind="lambda_parameter", name="rate")
    assert lam.origin is Origin.LOCAL and lam.value_type is None
    outside = project.one(PS, sig, kind="field", name="rate")
    assert outside.symbol_id == "com.acme.service.BasePricing#rate"
    decl = project.one(PS, sig, kind="lambda_parameter_declaration", name="rate")
    assert decl.status is R.UNRESOLVED and decl.reason == "inferred_type"
    assert project.call(sig, "rate.hashCode()").status is R.UNRESOLVED


def test_sibling_blocks_get_their_own_variable_types(project: Project) -> None:
    first = project.call("blocks(boolean)", "v.length()")
    assert (first.origin, first.owner_type) == (Origin.EXTERNAL, "java.lang.String")
    second = project.call("blocks(boolean)", "v.getName()")
    assert second.symbol_id == "com.acme.model.Customer#getName()"


def test_var_pattern_and_catch_variables(project: Project) -> None:
    sig = "tryVar(Object)"
    created = project.one(PS, sig, kind="local_variable_declaration", name="created")
    assert created.value_type is not None and created.value_type.fqn == "com.acme.model.Customer"
    assert project.call(sig, "created.getName()").status is R.RESOLVED  # `var` inferred from new
    text = project.one(PS, sig, kind="local_variable_declaration", name="text")
    assert text.value_type is not None and text.value_type.fqn == "java.lang.String"
    assert project.call(sig, "text.length()").owner_type == "java.lang.String"
    number = project.one(PS, sig, kind="local_variable_declaration", name="number")
    assert number.value_type is not None and number.value_type.fqn == "int"

    cust = project.call(sig, "cust.getStatus()")
    assert cust.symbol_id == "com.acme.model.Customer#getStatus()"  # pattern variable
    union = project.call(sig, "ex.getMessage()")  # multi-catch: no single static type
    assert union.status is R.UNRESOLVED and union.reason == "union_receiver"


def test_qualified_names_and_package_chains(project: Project) -> None:
    sig = "external(Customer)"
    typ = project.one(PS, sig, kind="type", text="java.util.Collections")
    assert (typ.owner_type, typ.origin) == ("java.util.Collections", Origin.EXTERNAL)
    packages = {r.name for r in project.refs(PS, sig, kind="package")}
    assert packages == {"java", "java.util"}  # segments upgraded once the chain ended in a type
    assert (
        project.call(sig, "java.util.Collections.emptyList()").owner_type == "java.util.Collections"
    )


# ---- calls ------------------------------------------------------------------------------------


def test_overloads_are_narrowed_by_argument_types(project: Project) -> None:
    sig = "overloads(Customer)"
    assert project.call(sig, "price(5)").symbol_id == f"{PS}#price(int)"
    assert project.call(sig, 'price("x")').symbol_id == f"{PS}#price(String)"
    assert project.call(sig, "price(owner)").symbol_id == f"{PS}#price(Customer)"  # via field type
    assert (
        project.call(sig, "price(c.getName())").symbol_id == f"{PS}#price(String)"
    )  # via call type
    assert project.call(sig, 'c.rename("x")').symbol_id == "com.acme.model.Customer#rename(String)"
    assert project.call(sig, "c.rename(c)").symbol_id == "com.acme.model.Customer#rename(Customer)"
    varargs = project.call(sig, 'Strings.join("a", "b", "c")')
    assert varargs.symbol_id == "com.acme.util.Strings#join(String...)"


def test_unnarrowable_overload_stays_ambiguous_and_wrong_arity_is_unresolved(
    project: Project,
) -> None:
    amb = project.call("problems(Customer)", "price(unknownCall())")
    assert amb.status is R.AMBIGUOUS and amb.reason == "overload_not_disambiguated"
    assert amb.symbol_id is None and len(amb.candidates) == 3
    arity = project.call("problems(Customer)", "price(1, 2)")
    assert arity.status is R.UNRESOLVED and arity.reason == "no_matching_arity"
    assert len(arity.candidates) == 3


def test_chained_calls_follow_return_types(project: Project) -> None:
    sig = "chains(Customer)"
    assert project.call(sig, "c.getStatus().isOpen()").symbol_id == "com.acme.model.Status#isOpen()"
    assert project.call(sig, "fresh.getName().length()").owner_type == "java.lang.String"
    plus = project.call(sig, "m.plus(m)")
    assert plus.symbol_id == "com.acme.model.Money#plus(Money)"
    assert plus.value_type is not None and plus.value_type.fqn == "com.acme.model.Money"


def test_records_and_enums_resolve_to_implicit_members(project: Project) -> None:
    sig = "chains(Customer)"
    ctor = project.one(PS, sig, kind="constructor", text='new Money(1L, "USD")')
    assert (ctor.symbol_id, ctor.implicit) == ("com.acme.model.Money#Money(long,String)", True)
    accessor = project.call(sig, "m.cents()")
    assert accessor.symbol_id == "com.acme.model.Money#cents()" and accessor.implicit
    assert accessor.value_type is not None and accessor.value_type.fqn == "long"
    constant = project.one(PS, sig, kind="enum_constant", name="ACTIVE")
    assert constant.symbol_id == "com.acme.model.Status#ACTIVE"
    values = project.call(sig, "Status.values()")
    assert values.implicit and values.value_type is not None and values.value_type.dims == 1


def test_inherited_and_default_methods_and_super_calls(project: Project) -> None:
    sig = "inherited()"
    assert project.call(sig, "base(3)").symbol_id == "com.acme.service.BasePricing#base(int)"
    assert project.call(sig, "super.base(4)").symbol_id == "com.acme.service.BasePricing#base(int)"
    assert project.call(sig, "this.price(1)").symbol_id == f"{PS}#price(int)"
    assert (
        project.call(sig, "audit()").symbol_id == "com.acme.service.BasePricing#audit()"
    )  # override wins
    assert (
        project.call(sig, "tag()").symbol_id == "com.acme.model.Auditable#tag()"
    )  # interface default
    obj = project.call(sig, "hashCode()")
    assert (obj.status, obj.origin, obj.owner_type) == (
        R.RESOLVED,
        Origin.EXTERNAL,
        "java.lang.Object",
    )


def test_static_calls_and_static_imports(project: Project) -> None:
    sig = "external(Customer)"
    assert project.call(sig, 'trim(" x ")').symbol_id == "com.acme.util.Strings#trim(String)"
    counting = project.call(sig, "counting()")
    assert (counting.origin, counting.owner_type) == (
        Origin.EXTERNAL,
        "java.util.stream.Collectors",
    )
    static = project.call("price(String)", "Strings.len(code)")
    assert static.symbol_id == "com.acme.util.Strings#len(String)"


def test_external_types_resolve_the_type_but_not_the_member(project: Project) -> None:
    sig = "external(Customer)"
    add = project.call(sig, "history.add(null)")
    assert (add.status, add.origin, add.owner_type) == (
        R.RESOLVED,
        Origin.EXTERNAL,
        "java.util.List",
    )
    assert add.symbol_id is None and add.file is None  # no source, so no verified member/location
    # the JDK's return types are unknown, so the chain stops there instead of guessing
    mapped = project.call(sig, "history.stream().map(x -> x.cents())")
    assert mapped.status is R.UNRESOLVED and mapped.reason == "receiver_type_unknown"
    println = project.call(sig, "System.out.println(c)")
    assert (println.owner_type, println.origin) == ("java.io.PrintStream", Origin.EXTERNAL)
    ctor = project.one(PS, sig, kind="constructor", text="new ArrayList<>(history)")
    assert (ctor.origin, ctor.owner_type) == (Origin.EXTERNAL, "java.util.ArrayList")


def test_unresolvable_names_carry_a_reason(project: Project) -> None:
    sig = "problems(Customer)"
    assert project.call(sig, "c.undefined()").reason == "no_such_method"
    assert project.call(sig, "unknownCall()", nth=0).reason == "no_such_method"
    widget = project.one(PS, "external(Customer)", kind="constructor", text="new Widget()")
    assert widget.status is R.UNRESOLVED and widget.reason == "external_unverifiable"
    assert widget.candidates == ["org.example.external.Widget"]


def test_lombok_generated_members_are_not_guessed(project: Project) -> None:
    sig = "problems(Customer)"
    getter = project.call(sig, "acc.getId()")
    assert getter.status is R.UNRESOLVED and getter.reason == "possible_lombok_generated"
    ctor = project.one(PS, sig, kind="constructor", text='new Account("x")')
    assert ctor.status is R.UNRESOLVED and ctor.reason == "possible_lombok_generated"
    default = project.one(PS, sig, kind="constructor", text="new Account()")
    assert default.status is R.RESOLVED and default.implicit


def test_type_variable_receivers_are_unresolved(project: Project) -> None:
    call = project.call("max(T,T)", "a.compareTo(b)")
    assert call.status is R.UNRESOLVED and call.reason == "type_variable_receiver"


def test_anonymous_class_bodies_are_not_pretended_away(project: Project) -> None:
    inner = project.call("scoping(Customer)", "base(1)")
    assert inner.status is R.AMBIGUOUS and inner.reason == "possible_local_class_member"
    assert inner.candidates == ["com.acme.service.BasePricing#base(int)"]


def test_nested_class_sees_outer_members(project: Project) -> None:
    helper = f"{PS}.Helper"
    limit = project.one(helper, "help(int)", kind="field", name="LIMIT")
    assert limit.symbol_id == f"{PS}#LIMIT"
    call = project.one(helper, "up()", kind="method", text="outerStatic(2)")
    assert call.symbol_id == f"{PS}#outerStatic(int)"
    own = project.one(helper, "up()", kind="method", text="help(1)")
    assert own.symbol_id == f"{helper}#help(int)"  # innermost class with a match wins


def test_method_references(project: Project) -> None:
    sig = "refs(List<Customer>)"
    assert project.one(PS, sig, kind="method_ref", text="Customer::getName").symbol_id == (
        "com.acme.model.Customer#getName()"
    )
    assert project.one(PS, sig, kind="method_ref", text="Strings::trim").symbol_id == (
        "com.acme.util.Strings#trim(String)"
    )
    assert project.one(PS, sig, kind="method_ref", text="this::inherited").symbol_id == (
        f"{PS}#inherited()"
    )
    overloaded = project.one(PS, sig, kind="method_ref", text="this::price")
    assert overloaded.status is R.AMBIGUOUS and len(overloaded.candidates) == 3
    ctor = project.one(PS, sig, kind="method_ref", text="Customer::new")
    assert ctor.status is R.AMBIGUOUS and ctor.reason == "overloaded_method_reference"


# ---- invariants over the whole project ---------------------------------------------------------


def test_every_non_resolved_reference_explains_itself(project: Project) -> None:
    refs = [r for c in project.classes.values() for m in c.methods for r in m.refs]
    assert len(refs) > 150
    missing = [r for r in refs if r.status is not R.RESOLVED and not r.reason]
    assert missing == []
    resolved_project = [
        r
        for r in refs
        if r.status is R.RESOLVED
        and r.origin is Origin.PROJECT
        and r.kind in {"method", "constructor", "field"}
    ]
    assert all(r.symbol_id and r.file and r.line for r in resolved_project)
    ambiguous = [r for r in refs if r.status is R.AMBIGUOUS]
    assert all(r.candidates for r in ambiguous)


def test_summary_counts_are_consistent(project: Project) -> None:
    resolutions = project.resolver.resolve_all(project.files)
    summary = project.resolver.summarize(resolutions)
    method_refs = sum(
        sum(counts.values()) for kind, counts in summary.by_kind.items() if kind == "method"
    )
    assert sum(summary.calls.values()) == method_refs
    assert summary.calls["project"] > 20 and summary.calls["external"] > 5
    assert summary.calls["ambiguous"] >= 3 and summary.calls["unresolved"] >= 5
    assert summary.unresolved_reasons["no_such_method"] >= 2
    assert summary.ambiguous_reasons["overload_not_disambiguated"] == 1  # price(unknownCall())
    assert summary.references == sum(sum(c.values()) for c in summary.by_kind.values())


# ---- robustness -------------------------------------------------------------------------------


def test_cyclic_inheritance_terminates(java_analyzer: JavaParserAnalyzer, tmp_path: Path) -> None:
    path = tmp_path / "Cycle.java"
    path.write_text("class A extends B { void f() { g(); } }\nclass B extends A { void g() {} }\n")
    (parsed,) = java_analyzer.analyze_files([path])
    files = [AnalyzedFile("1", "Cycle.java", parsed)]
    resolver = SymbolResolver(SymbolTable(files))
    out = {c.fqn: c for c in resolver.resolve_all(files)[0].classes}
    call = out["A"].methods[0].refs[0]
    assert call.symbol_id == "B#g()"  # still found; the cycle just does not recurse forever
    assert [c.name for c in resolver.hierarchy(resolver.table.lookup("A")[0]).classes] == ["A", "B"]


def test_extends_clause_naming_its_own_member_type_terminates(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    path = tmp_path / "Own.java"
    path.write_text("class A extends A.B { static class B {} }\n")
    (parsed,) = java_analyzer.analyze_files([path])
    files = [AnalyzedFile("1", "Own.java", parsed)]
    resolver = SymbolResolver(SymbolTable(files))
    out = resolver.resolve_all(files)[0].classes[0]
    assert out.superclass is not None and out.superclass.fqn == "A.B"  # right answer, no loop
    hierarchy = resolver.hierarchy(resolver.table.lookup("A")[0])
    assert [c.fqn for c in hierarchy.classes] == ["A", "A.B"]


def test_external_superclass_makes_missing_members_unresolved_not_wrong(
    java_analyzer: JavaParserAnalyzer, tmp_path: Path
) -> None:
    path = tmp_path / "Job.java"
    path.write_text(
        "package p;\nclass Job extends Thread {\n"
        "    int own;\n    void helper() {}\n"
        "    void run2() { start(); helper(); toString(); missing(); int x = own + inheritedField; }\n}\n"
    )
    (parsed,) = java_analyzer.analyze_files([path])
    files = [AnalyzedFile("1", "Job.java", parsed)]
    out = SymbolResolver(SymbolTable(files)).resolve_all(files)[0].classes[0]
    by_name = {r.name: r for r in out.methods[-1].refs if r.kind in {"method", "field", "name"}}
    assert by_name["helper"].symbol_id == "p.Job#helper()"
    assert by_name["toString"].owner_type == "java.lang.Object"
    for name in ("start", "missing"):  # Thread may define them, and we cannot see Thread
        assert by_name[name].status is R.UNRESOLVED
        assert by_name[name].reason == "possible_inherited_external_method"
    assert by_name["own"].symbol_id == "p.Job#own"
    assert by_name["inheritedField"].reason == "possible_inherited_external_field"

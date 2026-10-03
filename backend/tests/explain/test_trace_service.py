from __future__ import annotations

import pytest

from app.services.trace_service import TraceService, VariableNotFoundError, article
from tests.helpers import Env

PS = "com.acme.service.PricingService"


def pk(env: Env, suffix: str) -> str:
    return next(m.id for m in env.store.iter_methods(env.repo) if m.method_id.endswith(suffix))


def texts(result, direction: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [s.text for s in result.steps if s.direction == direction]


def test_a_parameter_is_traced_to_its_callers_and_back_out(project_env: Env) -> None:
    svc = TraceService(project_env.store)
    r = svc.trace(pk(project_env, "BasePricing#base(int)"), "x", depth=2)
    assert (r.variable, r.variable_kind, r.type) == ("x", "parameter", "int")
    assert texts(r, "upstream") == [
        "PricingService.inherited() passes an int literal as `x`",
        "PricingService.inherited() passes an int literal as `x`",
        "PricingService.price(int) passes parameter `units` as `x`",
        "PricingService.inherited() passes an int literal as `units`",
        "PricingService.overloads(Customer) passes an int literal as `units`",
    ]  # second level: where `units` came from in turn
    assert texts(r, "within") == ["created: parameter int x", "returned from the method"]
    assert texts(r, "downstream") == [
        "returned to the caller",
        "PricingService.price(int) returns it onward",
    ]
    assert r.stops == ["depth limit (2) reached above PricingService.price(int)"]


def test_depth_limits_how_far_the_trace_goes(project_env: Env) -> None:
    svc = TraceService(project_env.store)
    one = svc.trace(pk(project_env, "BasePricing#base(int)"), "x", depth=1)
    assert len(texts(one, "upstream")) == 3 and "units" not in " ".join(texts(one, "upstream")[3:])
    assert (
        "returned to the caller" in texts(one, "downstream") and len(texts(one, "downstream")) == 1
    )
    deep = svc.trace(pk(project_env, "BasePricing#base(int)"), "x", depth=3)
    assert len(deep.steps) >= len(one.steps)


def test_values_are_followed_into_callee_parameters(project_env: Env) -> None:
    r = TraceService(project_env.store).trace(
        pk(project_env, "PricingService#price(String)"), "code", depth=2
    )
    assert texts(r, "upstream") == [
        "PricingService.overloads(Customer) passes a String literal as `code`",
        "PricingService.overloads(Customer) passes the result of getName() as `code`",
        "PricingService.price(Customer) passes the result of getName() as `code`",
    ]
    assert "passed to len(s) (Strings.len(String))" in texts(r, "downstream")


def test_a_field_is_traced_to_its_writers_and_consumers(project_env: Env) -> None:
    r = TraceService(project_env.store).trace(
        pk(project_env, "Customer#getName()"), "name", depth=2
    )
    assert r.variable_kind == "field"
    assert texts(r, "upstream") == [
        "field `name` is modified in Customer.Customer(String)",
        "field `name` is modified in Customer.rename(Customer)",
        "field `name` is modified in Customer.rename(String)",
    ]
    down = texts(r, "downstream")
    assert "PricingService.chains(Customer) calls length() on it" in down
    assert "PricingService.overloads(Customer) passes it on (code)" in down


def test_every_step_that_has_a_location_has_a_citation(project_env: Env) -> None:
    r = TraceService(project_env.store).trace(
        pk(project_env, "BasePricing#base(int)"), "x", depth=2
    )
    labels = {c.label for c in r.citations}
    located = [s for s in r.steps if s.line]
    assert located and all(s.label in labels for s in located)
    assert all(s.file for s in located)
    cite = next(c for c in r.citations if c.label == located[0].label)
    assert (cite.file, cite.start_line) == (located[0].file, located[0].line)


def test_local_variables_are_traced_through_assignments_and_calls(env: Env) -> None:
    r = TraceService(env.store).trace(
        pk(env, "ShippingCalculator#shippingFee(double,boolean)"), "fee", depth=2
    )
    assert r.variable_kind == "local"
    assert texts(r, "upstream") == [
        "declare: from a double literal",
        "compound_assign: from an int literal",  # `fee += 20`
        "compound_assign: from local `fee`",  # ...which also reads fee itself
    ]
    within = texts(r, "within")
    assert within[0] == "created: initialized from double literal"
    assert "modified: update (+=): 20" in within and "returned from the method" in within
    assert "returned to the caller" in texts(r, "downstream")
    assert [
        x.kind for x in r.steps if x.kind == "used_by_caller"
    ] == []  # the test ignores the result


def test_unknown_variables_list_what_is_available(env: Env) -> None:
    with pytest.raises(VariableNotFoundError) as err:
        TraceService(env.store).trace(
            pk(env, "ShippingCalculator#loyaltyDiscount(double,int)"), "nope"
        )
    assert err.value.available == ["price", "years"]
    assert "available: price, years" in str(err.value)
    with pytest.raises(LookupError, match="unknown method"):
        TraceService(env.store).trace("m_missing", "x")


def test_text_rendering_for_the_prompt(project_env: Env) -> None:
    text = (
        TraceService(project_env.store)
        .trace(pk(project_env, "BasePricing#base(int)"), "x", depth=2)
        .as_text()
    )
    assert text.startswith("Subject: parameter `x` (int) in com.acme.service.BasePricing#base(int)")
    for heading in (
        "Where it comes from:",
        "Inside the method:",
        "Where it goes:",
        "The trace stops here:",
    ):
        assert heading in text
    assert (
        "    - PricingService.inherited() passes an int literal as `units`" in text
    )  # indented by depth


def test_cycles_do_not_loop(java_analyzer, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.knowledge.store import KnowledgeStore
    from tests.helpers import analyze_sources, build_knowledge

    src = {
        "p/Loop.java": "package p; public class Loop { int a(int n) { return b(n); } int b(int m) { return a(m); } }"
    }
    built = build_knowledge(analyze_sources(java_analyzer, tmp_path / "s", src))
    store = KnowledgeStore(tmp_path / "k.db")
    store.replace(built.repository, built.graph, "fp")
    a = next(m.id for m in built.repository.methods if m.name == "a")
    result = TraceService(store).trace(a, "n", depth=6)
    assert len(result.steps) < 40  # terminates, each (method, variable) visited once


def test_article() -> None:
    assert [article(w) for w in ("int", "String", "object", "Hour")] == ["an", "a", "an", "a"]

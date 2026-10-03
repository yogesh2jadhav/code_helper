from __future__ import annotations

import pytest

from app.config import Settings
from app.context.context_builder import ContextBuilder, ContextPackage
from app.context.tokens import estimate_tokens, truncate_to_tokens
from tests.helpers import Env

PS = "com.acme.service.PricingService"


def builder(
    env: Env, settings: Settings, *, retriever: bool = True, reader: bool = True
) -> ContextBuilder:
    return ContextBuilder(
        env.repo,
        env.store,
        env.reader if reader else None,
        settings,
        env.retriever if retriever else None,
    )


def titles(p: ContextPackage) -> list[str]:
    return [i.title for i in p.items]


# ---- tokens -----------------------------------------------------------------------------------


def test_token_estimates_are_conservative_and_truncation_is_line_based() -> None:
    assert estimate_tokens("") == 0 and estimate_tokens("abc") == 1
    assert estimate_tokens("x" * 350) == 100
    text = "\n".join(f"line number {i}" for i in range(50))
    cut, truncated = truncate_to_tokens(text, 40)
    assert truncated and estimate_tokens(cut) <= 40 + 10
    assert cut.split("\n")[-1].startswith("[... truncated]") and "more lines" in cut
    assert all(line.startswith("line number") for line in cut.split("\n")[:-1])  # no half lines
    assert truncate_to_tokens("short", 100) == ("short", False)


# ---- content and priority ---------------------------------------------------------------------


def test_sections_come_in_priority_order_with_dense_citation_labels(
    env: Env, isolated_settings: Settings
) -> None:
    pkg = builder(env, isolated_settings).build(env.method("shippingFee"))
    prio = [i.priority for i in pkg.items]
    assert prio == sorted(prio) and pkg.items[0].kind == "target_model"
    assert [i.kind for i in pkg.items[:3]] == ["target_model", "target_source", "flow"]
    assert pkg.complete and pkg.omitted == []

    labels = [c.label for c in pkg.citations]
    assert labels == [f"E{n}" for n in range(1, len(labels) + 1)]  # dense, no gaps
    by_label = {c.label: c for c in pkg.citations}
    source_item = next(i for i in pkg.items if i.kind == "target_source")
    cite = by_label[source_item.label or ""]
    assert (cite.file, cite.start_line, cite.end_line) == (
        "src/main/shop/ShippingCalculator.java",
        6,
        12,
    )
    assert f"[{source_item.label}]" in pkg.render()


def test_target_source_is_exact_and_numbered(env: Env, isolated_settings: Settings) -> None:
    pkg = builder(env, isolated_settings).build(env.method("shippingFee"))
    src = next(i for i in pkg.items if i.kind == "target_source")
    assert (
        src.text.split("\n")[0]
        == "6:     public double shippingFee(double weightKg, boolean express) {"
    )
    assert "9:             fee += 20;" in src.text and src.text.endswith("12:     }")
    assert not src.truncated


def test_analysis_summary_carries_inputs_outputs_unknowns(
    env: Env, isolated_settings: Settings
) -> None:
    pkg = builder(env, isolated_settings).build(env.method("shippingFee"))
    model = pkg.items[0].text
    assert "purpose [comment/fact]: Returns the shipping fee; heavy parcels cost extra." in model
    assert (
        "input double weightKg: ShippingCalculatorTest.heavyParcelsCostExtra() L6: literal int [test]"
        in model
    )
    assert "not established: the repository does not establish why 30 was chosen" in model


def test_rules_flow_tests_comments_callers_and_docs_are_included(
    env: Env, isolated_settings: Settings
) -> None:
    pkg = builder(env, isolated_settings).build(env.method("shippingFee"))
    kinds = {i.kind for i in pkg.items}
    assert {
        "target_model",
        "target_source",
        "flow",
        "rule",
        "types",
        "test",
        "comment",
        "caller",
    } <= kinds
    rules = next(i for i in pkg.items if i.kind == "rule").text
    assert "[threshold] when weightKg is greater than 30 -> set fee += 20 constants: 30" in rules
    test = next(i for i in pkg.items if i.kind == "test")
    assert (
        "shippingFee(40, false)" in test.text
        and test.source is not None
        and test.source[3] == "test"
    )


def test_flags_exclude_whole_categories(env: Env, isolated_settings: Settings) -> None:
    b = builder(env, isolated_settings)
    everything = {i.kind for i in b.build(env.method("shippingFee")).items}
    assert {"test", "comment", "caller"} <= everything
    pkg = b.build(
        env.method("shippingFee"), include_tests=False, include_docs=False, include_callers=False
    )
    assert {i.kind for i in pkg.items}.isdisjoint({"test", "comment", "doc", "caller"})
    assert {"target_model", "target_source", "rule"} <= {
        i.kind for i in pkg.items
    }  # code-derived stays


# ---- callees and depth ------------------------------------------------------------------------


def test_callees_depth_and_unresolved_groups(project_env: Env, isolated_settings: Settings) -> None:
    m = [x for x in project_env.store.find_method(project_env.repo, f"{PS}#price(Customer)")][0]
    b = ContextBuilder(project_env.repo, project_env.store, project_env.reader, isolated_settings)
    shallow = b.build(m, depth=1)
    callee = next(i for i in shallow.items if i.title == "Callee PricingService.price")
    assert "purpose [name]: performs price" in callee.text and "it calls:" not in callee.text
    deep = b.build(m, depth=2)
    assert (
        "it calls: len (performs len)"
        in next(i for i in deep.items if i.title == "Callee PricingService.price").text
    )
    assert any(i.title == "Callee Customer.getName" for i in deep.items)


def test_ambiguous_and_external_calls_are_summarised_not_dropped(
    project_env: Env, isolated_settings: Settings
) -> None:
    m = project_env.store.find_method(project_env.repo, f"{PS}#problems(Customer)")[0]
    pkg = ContextBuilder(
        project_env.repo, project_env.store, project_env.reader, isolated_settings
    ).build(m)
    amb = [i for i in pkg.items if i.title.startswith("Ambiguous call")]
    assert amb and "could be any of" in amb[0].text
    ext = [i for i in pkg.items if i.title.startswith("External calls")]
    assert not ext or "java" in ext[0].text  # JDK owners listed when present
    external = project_env.store.find_method(project_env.repo, f"{PS}#external(Customer)")[0]
    pkg2 = ContextBuilder(
        project_env.repo, project_env.store, project_env.reader, isolated_settings
    ).build(external)
    owners = next(i for i in pkg2.items if i.title.startswith("External calls")).text
    assert "java.util.List" in owners and "java.io.PrintStream" in owners


def test_dynamic_dispatch_is_noted_on_the_callee(
    java_analyzer, tmp_path, isolated_settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    from app.knowledge.source import SourceReader
    from app.knowledge.store import KnowledgeStore
    from tests.helpers import analyze_sources, build_knowledge

    src = {
        "p/Shape.java": "package p; public interface Shape { double area(); }",
        "p/Sq.java": "package p; public class Sq implements Shape { public double area() { return 1; } }",
        "p/Use.java": "package p; public class Use { double go(Shape s) { return s.area(); } }",
    }
    built = build_knowledge(analyze_sources(java_analyzer, tmp_path / "s", src))
    store = KnowledgeStore(tmp_path / "d.sqlite3")
    store.replace(built.repository, built.graph, "fp")
    go = next(m for m in built.repository.methods if m.name == "go")
    pkg = ContextBuilder(
        built.repository.repository_id, store, SourceReader(tmp_path / "s"), isolated_settings
    ).build(go)
    assert (
        "runtime may use an override: Sq" in next(i for i in pkg.items if i.kind == "callee").text
    )


# ---- budgets ----------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [150, 300, 600, 1200, 8000])
def test_the_package_never_exceeds_the_budget(
    env: Env, isolated_settings: Settings, limit: int
) -> None:
    pkg = builder(env, isolated_settings).build(env.method("shippingFee"), max_tokens=limit)
    assert pkg.total_tokens <= limit and pkg.budget.max_tokens == limit
    assert pkg.total_tokens == sum(i.tokens for i in pkg.items)
    assert sum(pkg.budget.used.values()) == pkg.total_tokens


def test_tight_budgets_drop_low_priority_items_first_and_say_so(
    env: Env, isolated_settings: Settings
) -> None:
    b = builder(env, isolated_settings)
    full = b.build(env.method("shippingFee"), max_tokens=8000)
    tight = b.build(env.method("shippingFee"), max_tokens=330)
    assert full.complete and not tight.complete
    kept = {i.kind: i for i in tight.items}
    assert {"target_model", "target_source"} <= set(kept)  # the essentials always survive
    assert kept["target_model"].truncated  # ...shortened rather than dropped
    assert kept["target_model"].text.startswith(
        "shop.ShippingCalculator.shippingFee(double,boolean)"
    )
    assert "purpose [comment/fact]" in kept["target_model"].text  # most important lines come first
    assert tight.omitted and "Target method (analysis) (shortened)" in tight.omitted
    dropped = {o for o in tight.omitted if not o.endswith("(shortened)")}
    assert dropped and all(t in titles(full) for t in dropped)
    assert max(i.priority for i in tight.items if i.kind in ("target_model", "target_source")) <= 2
    assert (
        min(i.priority for i in full.items if i.title in dropped) >= 5
    )  # only lower priorities go
    assert "Not included (context budget)" in tight.render()


def test_unused_share_spills_over_to_what_did_not_fit(
    env: Env, isolated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("CONTEXT_BUDGET_TARGET", "0.05")  # almost nothing reserved for the target...
    monkeypatch.setenv(
        "CONTEXT_BUDGET_CALLEES", "0.60"
    )  # ...but this method has no callees to use it
    get_settings.cache_clear()
    pkg = builder(env, get_settings()).build(env.method("shippingFee"), max_tokens=900)
    assert pkg.budget.allotted["target"] < 60
    assert [i.kind for i in pkg.items][:2] == [
        "target_model",
        "target_source",
    ]  # fits via spill-over
    assert not any(i.truncated for i in pkg.items if i.kind == "target_source")
    assert pkg.budget.used["target"] > pkg.budget.allotted["target"]


def test_budget_shares_are_renormalised(
    env: Env, isolated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    for name, value in (("TARGET", "7"), ("CALLEES", "1"), ("FLOW", "1"), ("EVIDENCE", "1")):
        monkeypatch.setenv(f"CONTEXT_BUDGET_{name}", value)
    get_settings.cache_clear()
    allotted = (
        builder(env, get_settings()).build(env.method("render"), max_tokens=1000).budget.allotted
    )
    assert allotted == {"target": 700, "callees": 100, "flow": 100, "evidence": 100}


LONG = (
    "package p;\npublic class Big {\n    int huge(int a) {\n        int r = 0;\n"
    + "".join(f"        if (a > {i}) {{ r += {i}; }}\n" for i in range(70))
    + "        return r;\n    }\n}\n"
)


def test_a_long_method_gets_an_outline_that_survives_truncation(
    java_analyzer, tmp_path, isolated_settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    from app.knowledge.source import SourceReader
    from app.knowledge.store import KnowledgeStore
    from tests.helpers import analyze_sources, build_knowledge

    built = build_knowledge(analyze_sources(java_analyzer, tmp_path / "s", {"p/Big.java": LONG}))
    store = KnowledgeStore(tmp_path / "big.sqlite3")
    store.replace(built.repository, built.graph, "fp")
    huge = next(m for m in built.repository.methods if m.name == "huge")
    b = ContextBuilder(
        built.repository.repository_id, store, SourceReader(tmp_path / "s"), isolated_settings
    )

    roomy = b.build(huge, max_tokens=8000)
    assert [i.title for i in roomy.items if i.kind == "flow"][
        0
    ] == "Target method structure (outline)"
    assert roomy.complete

    mid = b.build(huge, max_tokens=1500)  # outline fits whole; the source is cut at a line boundary
    source = next(i for i in mid.items if i.kind == "target_source")
    assert source.truncated and "more lines" in source.text and not mid.complete
    assert any(
        i.title == "Target method structure (outline)" and not i.truncated for i in mid.items
    )

    tight = b.build(huge, max_tokens=700)  # not even a useful fragment of source fits
    assert any(i.title == "Target method structure (outline)" for i in tight.items)
    assert "Target method source" in tight.omitted and tight.total_tokens <= 700


# ---- retrieval, source fallback, determinism --------------------------------------------------


def test_retrieved_extras_are_added_and_exclude_the_target(
    env: Env, isolated_settings: Settings
) -> None:
    pkg = builder(env, isolated_settings).build(
        env.method("loyaltyDiscount"), query="shipping fee for heavy parcels"
    )
    related = [i for i in pkg.items if i.kind == "retrieved"]
    assert related and all(i.title.startswith("Related") for i in related)
    assert not any("loyaltyDiscount" in i.title for i in related)
    assert len(related) <= 4
    without = builder(env, isolated_settings, retriever=False).build(env.method("loyaltyDiscount"))
    assert not [i for i in without.items if i.kind == "retrieved"]


class ExplodingRetriever:
    def search(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("index corrupt")


def test_a_failing_retriever_does_not_fail_the_explanation(
    env: Env, isolated_settings: Settings
) -> None:
    b = ContextBuilder(env.repo, env.store, env.reader, isolated_settings, ExplodingRetriever())  # type: ignore[arg-type]
    pkg = b.build(env.method("shippingFee"))
    assert pkg.items and not [i for i in pkg.items if i.kind == "retrieved"]


def test_without_a_reader_the_stored_snippet_is_used(env: Env, isolated_settings: Settings) -> None:
    pkg = builder(env, isolated_settings, reader=False).build(env.method("shippingFee"))
    src = next(i for i in pkg.items if i.kind == "target_source")
    assert "fee += 20;" in src.text


def test_building_twice_is_identical(env: Env, isolated_settings: Settings) -> None:
    b = builder(env, isolated_settings)
    one, two = b.build(env.method("shippingFee")), b.build(env.method("shippingFee"))
    assert one.model_dump_json() == two.model_dump_json()
    assert ContextPackage.model_validate_json(one.model_dump_json()) == one

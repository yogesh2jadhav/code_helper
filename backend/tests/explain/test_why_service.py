from __future__ import annotations

import pytest

from app.knowledge.source import SourceReader
from app.services.why_service import WhyService
from tests.helpers import Env

FEE = "ShippingCalculator#shippingFee"


def service(env: Env) -> WhyService:
    return WhyService(env.store, env.reader)


def texts(points) -> list[str]:  # type: ignore[no-untyped-def]
    return [p.text for p in points]


def test_selected_threshold_is_explained_with_confirmed_likely_and_unknown(env: Env) -> None:
    r = service(env).why(env.method("shippingFee").id, 8, 10)
    assert (r.start_line, r.end_line) == (8, 10)
    assert r.selection == "        if (weightKg > 30) {\n            fee += 20;\n        }"
    assert texts(r.confirmed) == [
        "At L8 the code applies: weightKg is greater than 30; then set fee += 20",
        "Test ShippingCalculatorTest.heavyParcelsCostExtra calls shippingFee(40, false)",
    ]
    assert r.unknown[0] == "why 30 was chosen: no comment, test or document mentions it"
    assert "no comment within 3 lines of L8-10 explains the intent" in r.unknown
    assert r.unknown[-1] == "why this logic was introduced (git history is not analyzed)"


def test_nothing_is_confirmed_beyond_what_the_evidence_says(env: Env) -> None:
    r = service(env).why(env.method("shippingFee").id, 8, 10)
    for point in r.confirmed:  # confirmed points are code facts or author-written artifacts
        assert point.text.startswith(
            ("At L", "Test ", "A nearby comment", "The method's doc", "README")
        )
    likely = texts(r.likely)
    assert (
        'The test is named "heavy parcels cost extra", which suggests the behaviour it checks is intended'
        in likely
    )
    assert any(
        t.endswith("(general documentation of the method, not about these lines specifically)")
        for t in likely
    )
    assert (
        "L8 separates cases by a numeric cut-off, a common way to encode a tier or limit (a threshold pattern)"
        in likely
    )
    assert not any("because" in t.lower() for t in texts(r.confirmed))  # no invented reasons


def test_a_stated_reason_is_confirmed_and_removes_the_unknown(java_analyzer, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.knowledge.store import KnowledgeStore
    from tests.helpers import analyze_sources, build_knowledge

    src = """package p;
public class Policy {
    boolean eligible(int age) {
        // minimum age is 18 because of the licensing rules
        if (age < 18) {
            return false;
        }
        return age > 77;
    }
}
"""
    built = build_knowledge(analyze_sources(java_analyzer, tmp_path / "s", {"p/Policy.java": src}))
    store = KnowledgeStore(tmp_path / "k.db")
    store.replace(built.repository, built.graph, "fp")
    pk = next(m.id for m in built.repository.methods if m.name == "eligible")
    why = WhyService(store, SourceReader(tmp_path / "s"))

    explained = why.why(pk, 5, 7)
    assert (
        'A nearby comment (L4) says: "minimum age is 18 because of the licensing rules"'
        in texts(explained.confirmed)
    )
    assert not any("why 18" in u for u in explained.unknown)  # the comment mentions 18
    assert not any("explains the intent" in u for u in explained.unknown)

    unexplained = why.why(pk, 8, 8)
    assert any(
        u == "why 77 was chosen: no comment, test or document mentions it"
        for u in unexplained.unknown
    )
    assert any("no comment within 3 lines of L8-8" in u for u in unexplained.unknown)
    assert any("no test exercises this method" in u for u in unexplained.unknown)


def test_default_selection_is_the_whole_method_and_is_clamped(env: Env) -> None:
    m = env.method("shippingFee")
    whole = service(env).why(m.id)
    assert (whole.start_line, whole.end_line) == (m.start_line, m.end_line)
    clamped = service(env).why(m.id, 1, 999)
    assert (clamped.start_line, clamped.end_line) == (m.start_line, m.end_line)


def test_a_selection_without_rules_describes_what_is_there(env: Env) -> None:
    r = service(env).why(env.method("render").id)
    assert texts(r.confirmed) == ['The selected lines do: return customer + ": " + total']
    assert r.likely == []  # no rule pattern, no docs, no tests, no callers: nothing to infer from


def test_errors(env: Env) -> None:
    with pytest.raises(LookupError, match="unknown method"):
        service(env).why("m_nope")
    m = env.method("shippingFee")
    with pytest.raises(ValueError, match="outside"):
        service(env).why(m.id, m.end_line + 5, m.end_line + 9)


def test_citations_resolve_and_text_rendering(env: Env) -> None:
    r = service(env).why(env.method("shippingFee").id, 8, 10)
    labels = {c.label for c in r.citations}
    assert all(set(p.refs) <= labels for p in [*r.confirmed, *r.likely])
    assert any(c.source_type == "test" for c in r.citations)
    text = r.as_text()
    assert (
        text.splitlines()[0]
        == "Selected: shop.ShippingCalculator#shippingFee(double,boolean) L8-10"
    )
    assert "CONFIRMED (code or author-written evidence):" in text
    assert "LIKELY (inference):" in text and "UNKNOWN (not established by the repository):" in text


def test_without_a_reader_the_selection_comes_from_the_stored_snippet(env: Env) -> None:
    r = WhyService(env.store, None).why(env.method("shippingFee").id, 9, 9)
    assert r.selection.strip() == "fee += 20;"

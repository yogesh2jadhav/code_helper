from __future__ import annotations

from pathlib import Path

from app.analyzer.ast_models import Comment
from app.knowledge.evidence import (
    DocIndex,
    comment_evidence,
    doc_evidence,
    first_sentence,
    is_test_location,
    load_docs,
    make_evidence,
)


def comment(kind: str, text: str, line: int = 1) -> Comment:
    return Comment(kind=kind, start_line=line, end_line=line, text=text)  # type: ignore[arg-type]


def test_evidence_ids_are_stable_and_distinguish_location_and_relation() -> None:
    a = make_evidence("source_code", "A.java", 3, 9, "code", "implements", method="m")
    b = make_evidence("source_code", "A.java", 3, 9, "other snippet", "implements", method="m")
    assert a.id == b.id and len(a.id) == 12  # the snippet is not part of the identity
    assert make_evidence("source_code", "A.java", 3, 10, "x", "implements", method="m").id != a.id
    assert make_evidence("comment", "A.java", 3, 9, "x", "implements", method="m").id != a.id
    assert make_evidence("source_code", "A.java", 3, 9, "x", "documents", method="m").id != a.id


def test_snippets_are_bounded() -> None:
    e = make_evidence("source_code", "A.java", 1, 2, "x" * 5000, "implements")
    assert len(e.snippet) == 800


def test_comment_evidence_classifies_javadoc_inline_and_commented_out_code() -> None:
    out = comment_evidence(
        [
            comment("javadoc", "Sums the lines."),
            comment("line", "round up per policy", 5),
            comment("line", "total += tax;", 6),
            comment("block", "if (x > 0) {", 7),
            comment("line", "   ", 8),
        ],
        "A.java",
        "A",
        "total",
    )
    assert [(e.relation, e.confidence) for e in out] == [
        ("documents", "high"),
        ("explains", "medium"),
        ("commented_code", "low"),
        ("commented_code", "low"),
    ]
    assert all(e.source_type == "comment" and e.class_name == "A" for e in out)


def test_first_sentence_strips_javadoc_markup() -> None:
    text = "Computes the {@code total} of <b>all</b> lines. Rounds up.\n@param lines the lines\n@return sum"
    assert first_sentence(text) == "Computes the total of all lines."
    assert first_sentence("No full stop here") == "No full stop here"
    assert first_sentence("Is valid?  Yes") == "Is valid?"


def test_test_locations() -> None:
    assert is_test_location("src/test/java/com/acme/FooTest.java", "com.acme.FooTest")
    assert is_test_location("src/main/java/com/acme/Foo.java", "com.acme.FooTests")
    assert is_test_location("src/main/java/Foo.java", "Foo", ["Test"])
    assert is_test_location("tests/Foo.java", "Foo")
    assert not is_test_location("src/main/java/com/acme/Foo.java", "com.acme.Foo")
    assert not is_test_location("src/main/java/Contest.java", "Contest")  # "Test" inside a word


def test_docs_are_split_into_sections_and_found_by_mention(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Shop\nIntro text.\n\n## Pricing\nThe PricingService decides discounts.\n"
        "Call `PricingService.price(` to compute.\n\n## Other\nNothing relevant.\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "design.md").write_text("# Design\nOrderCalculator sums lines.\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "README.md").write_text("# Ignored\nPricingService\n")
    (tmp_path / "notes.md").write_text("# Not a doc\nPricingService\n")  # outside README/docs

    sections = load_docs(tmp_path)
    assert [(s.file, s.heading) for s in sections] == [
        ("README.md", "Shop"),
        ("README.md", "Pricing"),
        ("README.md", "Other"),
        ("docs/design.md", "Design"),
    ]
    index = DocIndex(sections)
    hits = index.mentions_class("PricingService")
    assert [(s.file, s.heading, s.start_line) for s in hits] == [("README.md", "Pricing", 4)]
    assert [s.file for s in index.mentions_class("OrderCalculator")] == ["docs/design.md"]
    assert index.mentions_class("Foo") == []  # too short to be a meaningful mention
    assert [s.heading for s in index.mentions_method("PricingService", "price")] == ["Pricing"]
    assert index.mentions_method("PricingService", "decides") == []  # no call syntax

    evidence = doc_evidence(hits, "PricingService", None, "medium")
    assert (evidence[0].source_type, evidence[0].relation) == ("readme", "mentions")
    assert evidence[0].start_line == 4 and "decides discounts" in evidence[0].snippet
    assert (
        doc_evidence(index.mentions_class("OrderCalculator"), None, None, "medium")[0].source_type
        == "documentation"
    )


def test_unreadable_or_oversized_docs_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_bytes(b"\xff\xfe not utf8")
    (tmp_path / "README_big.md").write_text("# Big\n" + "x" * 300)
    assert load_docs(tmp_path, max_bytes=100) == []

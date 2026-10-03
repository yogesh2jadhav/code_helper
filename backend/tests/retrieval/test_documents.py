from __future__ import annotations

from app.knowledge.source import SourceReader
from app.retrieval.documents import Document, build_documents
from tests.shop_repo import World


def docs_of(world: World, with_source: bool = True) -> list[Document]:
    k = world.built.repository
    reader = SourceReader(world.root) if with_source else None
    return list(build_documents(world.repository_id, k.classes, k.methods, reader))


def of_type(docs: list[Document], kind: str) -> list[Document]:
    return [d for d in docs if d.type == kind]


def test_each_kind_is_a_separate_document_type(shop: World) -> None:
    docs = docs_of(shop)
    counts = {
        k: len(of_type(docs, k))
        for k in (
            "method_source",
            "class_source",
            "rule_candidate",
            "evidence",
            "test",
            "documentation",
        )
    }
    assert counts == {
        "method_source": 3,  # shippingFee, loyaltyDiscount, render
        "class_source": 3,
        "rule_candidate": 3,
        "evidence": 3,  # the class javadoc and two method javadocs
        "documentation": 2,  # the two README sections that name a class
        "test": 1,
    }
    assert len({d.id for d in docs}) == len(docs)  # ids are unique


def test_method_documents_carry_exact_source_and_metadata(shop: World) -> None:
    doc = next(
        d for d in docs_of(shop) if d.type == "method_source" and d.method_name == "shippingFee"
    )
    assert "double shippingFee(double weightKg, boolean express)" in doc.text
    assert "purpose: Returns the shipping fee; heavy parcels cost extra." in doc.text
    assert "fee += 20;" in doc.text  # the real source, read from disk
    assert (doc.class_name, doc.file_path) == (
        "shop.ShippingCalculator",
        "src/main/shop/ShippingCalculator.java",
    )
    assert doc.symbol_ids == ["shop.ShippingCalculator#shippingFee(double,boolean)"]
    assert doc.line_start == 6 and doc.line_end == 12
    meta = doc.metadata()
    assert all(isinstance(v, str | int | float | bool) for v in meta.values())
    assert meta["source_type"] == "method_source" and meta["symbol_ids"] == doc.symbol_ids[0]


def test_without_a_reader_the_stored_snippet_is_used(shop: World) -> None:
    doc = next(d for d in docs_of(shop, with_source=False) if d.method_name == "loyaltyDiscount")
    assert "years >= 5" in doc.text


def test_tests_rules_and_class_summaries(shop: World) -> None:
    docs = docs_of(shop)
    test = next(d for d in of_type(docs, "test") if d.method_name == "heavyParcelsCostExtra")
    assert "shippingFee(40, false)" in test.text and test.id.startswith("ts:")
    rule = next(d for d in of_type(docs, "rule_candidate") if "weightKg > 30" in d.text)
    assert rule.method_name == "shippingFee" and rule.id.startswith("rc:")
    assert "threshold" in rule.text and "weightKg is greater than 30" in rule.text
    cls = next(
        d for d in of_type(docs, "class_source") if d.class_name == "shop.ShippingCalculator"
    )
    assert (
        "shippingFee(double,boolean)" in cls.text
        and "Computes shipping cost for parcels." in cls.text
    )


def test_shared_evidence_is_indexed_once_with_all_symbols(shop: World) -> None:
    docs = docs_of(shop)
    readme = [d for d in of_type(docs, "documentation") if "ShippingCalculator" in d.text]
    assert len(readme) == 1
    assert "shop.ShippingCalculator" in readme[0].symbol_ids  # class-level mention is linked
    assert readme[0].file_path == "README.md"


def test_hash_tracks_content_not_identity(shop: World) -> None:
    doc = docs_of(shop)[0]
    same = doc.model_copy()
    changed = doc.model_copy(update={"text": doc.text + " more"})
    moved = doc.model_copy(update={"line_start": doc.line_start + 1})
    assert doc.hash == same.hash and len(doc.hash) == 16
    assert changed.hash != doc.hash and moved.hash != doc.hash
    assert docs_of(shop) == docs_of(shop)  # deterministic


def test_source_reader_refuses_to_leave_the_repository(shop: World) -> None:
    reader = SourceReader(shop.root)
    assert reader.text("README.md") is not None
    assert (
        reader.lines("src/main/shop/InvoicePrinter.java", 3, 3) == "public class InvoicePrinter {"
    )
    assert reader.text("../../etc/passwd") is None and reader.path("../outside.txt") is None
    assert reader.text("missing.java") is None and reader.lines("missing.java", 1, 2) is None

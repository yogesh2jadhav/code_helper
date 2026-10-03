"""A small purpose-built repository (sources, a test and a README) shared by several test suites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.knowledge.builder import BuiltKnowledge

SHIPPING = """package shop;

/** Computes shipping cost for parcels. */
public class ShippingCalculator {
    /** Returns the shipping fee; heavy parcels cost extra. */
    public double shippingFee(double weightKg, boolean express) {
        double fee = 5.0;
        if (weightKg > 30) {
            fee += 20;
        }
        return express ? fee * 2 : fee;
    }

    /** Applies the loyalty discount to a price. */
    public double loyaltyDiscount(double price, int years) {
        return years >= 5 ? price * 0.9 : price;
    }
}
"""
INVOICE = """package shop;

public class InvoicePrinter {
    public String render(String customer, double total) {
        return customer + ": " + total;
    }
}
"""
TEST = """package shop;

public class ShippingCalculatorTest {
    @Test
    void heavyParcelsCostExtra() {
        new ShippingCalculator().shippingFee(40, false);
    }
}
"""
README = """# Shop

## Shipping
The ShippingCalculator charges extra for heavy parcels and doubles the fee for express delivery.

## Printing
Invoices are rendered by InvoicePrinter.
"""


@dataclass
class World:
    root: Path
    built: BuiltKnowledge

    @property
    def repository_id(self) -> str:
        return self.built.repository.repository_id


SHOP_FILES = {
    "src/main/shop/ShippingCalculator.java": SHIPPING,
    "src/main/shop/InvoicePrinter.java": INVOICE,
    "src/test/shop/ShippingCalculatorTest.java": TEST,
    "README.md": README,
}

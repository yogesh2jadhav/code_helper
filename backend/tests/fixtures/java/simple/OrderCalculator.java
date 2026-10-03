package com.example.fixtures.simple;

import java.util.List;

/** Totals up order lines. */
public class OrderCalculator {
    private static final double TAX_RATE = 0.08;
    private final String currency;

    public OrderCalculator(String currency) {
        this.currency = currency;
    }

    /**
     * Sum of price * quantity across lines.
     */
    public double calculateTotal(List<OrderLine> lines) {
        double total = 0;
        for (OrderLine line : lines) {
            total += line.price() * line.quantity(); // line subtotal
        }
        return total;
    }

    public double withTax(double amount) {
        return applyRate(amount, TAX_RATE);
    }

    private static double applyRate(double amount, double rate) {
        return amount * (1 + rate);
    }

    public String getCurrency() {
        return currency;
    }

    public record OrderLine(String sku, double price, int quantity) {}
}

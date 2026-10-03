package com.example.fixtures.zero;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public class InvoiceBatch {
    public List<Summary> process(List<Row> rows, int threshold, boolean strict) {
        List<Row> kept = new ArrayList<>();
        for (Row row : rows) {
            if (row.account() == null) {
                if (strict) {
                    throw new IllegalStateException("missing account");
                }
                continue;
            }
            if (row.amount() > threshold && row.region().equals("EU")) {
                kept.add(row.withAmount(row.amount() * 2));
            } else {
                kept.add(row);
            }
        }
        Map<String, Double> byAccount = kept.stream()
                .collect(Collectors.groupingBy(Row::account, Collectors.summingDouble(Row::amount)));
        return byAccount.entrySet().stream()
                .map(e -> new Summary(e.getKey(), e.getValue()))
                .filter(s -> s.total() > 0)
                .collect(Collectors.toList());
    }

    public record Row(String account, double amount, String region) {
        Row withAmount(double a) {
            return new Row(account, a, region);
        }
    }

    public record Summary(String account, double total) {}
}

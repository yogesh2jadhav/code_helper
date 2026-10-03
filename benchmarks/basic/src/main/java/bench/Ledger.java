package bench;

import java.util.List;

public class Ledger {
    public double settle(List<Double> entries, double limit) {
        double balance = 0;
        for (double entry : entries) {
            if (entry < 0) {
                balance = balance + entry;
            } else if (balance + entry > limit) {
                balance = limit;
            } else {
                balance = balance + entry;
            }
        }
        return balance;
    }
}

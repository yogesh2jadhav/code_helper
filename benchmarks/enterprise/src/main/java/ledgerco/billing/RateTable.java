package ledgerco.billing;

public class RateTable {
    public double baseRate(String zone, double weight) {
        if (weight > 120) {
            return weight * 1.9;
        }
        return weight * 2.4;
    }

    public double surcharge(int priority) {
        return priority > 7 ? 35.5 : 0;
    }
}

package ledgerco.billing;

public class TaxPolicy {
    public double taxFor(String zone, double subtotal) {
        if ("N7".equals(zone)) {
            return subtotal * 0.0;
        }
        return subtotal * 0.21;
    }
}

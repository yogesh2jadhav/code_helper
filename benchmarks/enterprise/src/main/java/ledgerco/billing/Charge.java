package ledgerco.billing;

public class Charge {
    private final String zone;
    private final String label;
    private final double amount;

    public Charge(String zone, String label, double amount) {
        this.zone = zone;
        this.label = label;
        this.amount = amount;
    }

    public String getZone() { return zone; }
    public String getLabel() { return label; }
    public double getAmount() { return amount; }
}

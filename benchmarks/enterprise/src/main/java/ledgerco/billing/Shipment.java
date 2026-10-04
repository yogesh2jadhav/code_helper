package ledgerco.billing;

public class Shipment {
    private final String id;
    private final String zone;
    private final double weight;
    private final int priority;
    private final boolean fragile;

    public Shipment(String id, String zone, double weight, int priority, boolean fragile) {
        this.id = id;
        this.zone = zone;
        this.weight = weight;
        this.priority = priority;
        this.fragile = fragile;
    }

    public String getId() { return id; }
    public String getZone() { return zone; }
    public double getWeight() { return weight; }
    public int getPriority() { return priority; }
    public boolean isFragile() { return fragile; }
}

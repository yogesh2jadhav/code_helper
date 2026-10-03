package bench;

public class Order {
    private final String region;
    private final double amount;
    private final boolean cancelled;

    public Order(String region, double amount, boolean cancelled) {
        this.region = region;
        this.amount = amount;
        this.cancelled = cancelled;
    }

    public String getRegion() {
        return region;
    }

    public double getAmount() {
        return amount;
    }

    public boolean isCancelled() {
        return cancelled;
    }
}

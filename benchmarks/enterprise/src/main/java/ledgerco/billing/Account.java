package ledgerco.billing;

public class Account {
    private final String code;
    private final int tier;
    private double exposure;

    public Account(String code, int tier) {
        this.code = code;
        this.tier = tier;
    }

    public String getCode() { return code; }
    public int getTier() { return tier; }
    public double getExposure() { return exposure; }
    public void addExposure(double delta) { this.exposure = this.exposure + delta; }
}

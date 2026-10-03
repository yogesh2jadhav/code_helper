package bench;

public class Discounts {
    public double discount(double amount, int years) {
        if (amount > 500) {
            return amount * 0.10;
        }
        if (years >= 3) {
            return amount * 0.05;
        }
        return 0;
    }
}

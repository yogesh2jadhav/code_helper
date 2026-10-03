package ledgerco.billing;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public class BatchBiller {
    private final RateTable rates;
    private final TaxPolicy taxes;

    public BatchBiller(RateTable rates, TaxPolicy taxes) {
        this.rates = rates;
        this.taxes = taxes;
    }

    public double bill(Account account, List<Shipment> shipments, boolean rush, double creditCeiling) {
        List<Charge> charges = new ArrayList<>();
        for (Shipment s : shipments) {
            if (s.getWeight() <= 0) {
                continue;
            }
            double base = rates.baseRate(s.getZone(), s.getWeight());
            if (s.isFragile()) {
                base = base * 1.15;
            }
            if (rush && s.getPriority() > 3) {
                base = base + rates.surcharge(s.getPriority());
            } else if (account.getTier() >= 4) {
                base = base * 0.92;
            }
            charges.add(new Charge(s.getZone(), s.getId(), base));
        }

        Map<String, Double> perZone = charges.stream()
                .filter(c -> c.getAmount() > 14)
                .collect(Collectors.groupingBy(Charge::getZone, Collectors.summingDouble(Charge::getAmount)));

        double total = 0;
        for (Map.Entry<String, Double> entry : perZone.entrySet()) {
            double subtotal = entry.getValue();
            double tax = taxes.taxFor(entry.getKey(), subtotal);
            total = total + subtotal + tax;
        }

        if (total > creditCeiling) {
            total = creditCeiling;
        }
        if (account.getExposure() + total > 25000) {
            total = total * 0.5;
        }
        account.addExposure(total);
        return total;
    }
}

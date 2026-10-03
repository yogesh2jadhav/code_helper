package com.example.fixtures.branches;

public class ShippingPolicy {
    public String classify(Parcel parcel, boolean express) {
        if (parcel == null) {
            return "INVALID";
        } else if (parcel.weightKg() > 30 || parcel.lengthCm() > 150) {
            if (express && parcel.weightKg() <= 50) {
                return "FREIGHT_EXPRESS";
            } else {
                return "FREIGHT";
            }
        } else if (parcel.weightKg() >= 10) {
            return "HEAVY";
        } else {
            String label = express ? "STANDARD_EXPRESS" : "STANDARD";
            return label;
        }
    }

    public boolean isMissing(String code) {
        return code != null && !code.isEmpty() ? false : true;
    }

    public record Parcel(double weightKg, double lengthCm) {}
}

package com.example.fixtures.switches;

public class SwitchSamples {
    public int legacyRate(String category) {
        int rate;
        switch (category) {
            case "A":
                rate = 10;
                break;
            case "B":
            case "C":
                rate = 20;
                break;
            default:
                rate = 0;
        }
        return rate;
    }

    public String modernLabel(int level) {
        return switch (level) {
            case 1 -> "LOW";
            case 2 -> "MEDIUM";
            default -> "HIGH";
        };
    }

    public int parseOrDefault(String text, int fallback) {
        try {
            return Integer.parseInt(text);
        } catch (NumberFormatException e) {
            return fallback;
        } finally {
            System.out.println("parsed");
        }
    }

    public void requirePositive(int value) {
        if (value <= 0) {
            throw new IllegalArgumentException("value must be positive");
        }
    }
}

package bench;

public class Converter {
    public Quantity convert(String raw) {
        String trimmed = raw.trim();
        int parsed = Integer.parseInt(trimmed);
        Quantity result = new Quantity(parsed);
        return result;
    }
}

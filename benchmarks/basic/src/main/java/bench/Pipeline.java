package bench;

public class Pipeline {
    public int run(String raw) {
        String cleaned = normalize(raw);
        if (!isValid(cleaned)) {
            return -1;
        }
        return store(cleaned);
    }

    private String normalize(String raw) {
        return raw.trim().toLowerCase();
    }

    private boolean isValid(String text) {
        return !text.isEmpty();
    }

    private int store(String text) {
        return text.length();
    }
}

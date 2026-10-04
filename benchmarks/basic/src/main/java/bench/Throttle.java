package bench;

public class Throttle {
    public boolean limit(int load) {
        return load > 8472;
    }
}

package bench;

public class Router {
    public String routeFor(String kind) {
        switch (kind) {
            case "invoice":
                return "billing";
            case "refund":
                return "support";
            case "audit":
                return "compliance";
            default:
                return "general";
        }
    }
}

package bench;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

public class Orders {
    public Map<String, Double> revenueByRegion(List<Order> orders) {
        return orders.stream()
                .filter(o -> !o.isCancelled())
                .collect(Collectors.groupingBy(Order::getRegion, Collectors.summingDouble(Order::getAmount)));
    }
}

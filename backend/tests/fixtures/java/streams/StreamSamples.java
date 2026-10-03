package com.example.fixtures.streams;

import static java.util.stream.Collectors.counting;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.stream.Collectors;

public class StreamSamples {
    public record Visit(String patientId, String facility, int days, String status) {}

    public Map<String, List<Visit>> openVisitsByFacility(List<Visit> visits) {
        return visits.stream()
                .filter(Objects::nonNull)
                .filter(v -> !"CLOSED".equals(v.status()))
                .collect(Collectors.groupingBy(Visit::facility));
    }

    public Map<String, Long> countByStatus(List<Visit> visits) {
        return visits.stream().collect(Collectors.groupingBy(Visit::status, counting()));
    }

    public int longestStay(List<Visit> visits) {
        return visits.stream().mapToInt(Visit::days).max().orElse(0);
    }

    public String facilityOf(Visit visit) {
        return Optional.ofNullable(visit)
                .map(Visit::facility)
                .filter(f -> !f.isBlank())
                .orElse("UNKNOWN");
    }

    public boolean anyLong(List<Visit> visits) {
        return visits.stream().anyMatch(v -> v.days() > 14);
    }
}

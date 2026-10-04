package com.acme.model;

public interface Auditable {
    String audit();

    default String tag() {
        return "audit";
    }
}

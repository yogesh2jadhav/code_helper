package com.acme.model;

public enum Status {
    ACTIVE,
    CLOSED;

    public boolean isOpen() {
        return this == ACTIVE;
    }
}

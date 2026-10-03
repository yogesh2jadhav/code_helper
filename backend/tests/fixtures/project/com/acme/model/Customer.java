package com.acme.model;

public class Customer {
    private String name;
    private Status status;

    public Customer() {}

    public Customer(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public Status getStatus() {
        return status;
    }

    public void rename(String newName) {
        this.name = newName;
    }

    public void rename(Customer other) {
        this.name = other.name;
    }
}

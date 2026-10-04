package com.acme.service;

import com.acme.model.Auditable;

public abstract class BasePricing implements Auditable {
    protected int rate;

    protected int base(int x) {
        return x * rate;
    }

    public String audit() {
        return "base";
    }
}

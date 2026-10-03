package com.acme.service;

import static com.acme.util.Strings.trim;
import static java.util.stream.Collectors.counting;

import com.acme.a.*;
import com.acme.b.*;
import com.acme.dup.Thing;
import com.acme.lombok.Account;
import com.acme.model.*;
import com.acme.util.Strings;
import java.util.ArrayList;
import java.util.List;
import org.example.external.*;

public class PricingService extends BasePricing {
    private static final int LIMIT = 10;
    private final Customer owner;
    private List<Money> history = new ArrayList<>();

    public PricingService(Customer owner) {
        this.owner = owner;
    }

    public int price(int units) {
        return base(units) * LIMIT;
    }

    public int price(String code) {
        return Strings.len(code);
    }

    public int price(Customer customer) {
        return price(customer.getName());
    }

    public void overloads(Customer c) {
        int a = price(5);
        int b = price("x");
        int d = price(owner);
        int e = price(c.getName());
        c.rename("x");
        c.rename(c);
        String joined = Strings.join("a", "b", "c");
    }

    public void chains(Customer c) {
        c.getStatus().isOpen();
        Status s = Status.ACTIVE;
        Money m = new Money(1L, "USD");
        m.plus(m);
        long cents = m.cents();
        Customer fresh = new Customer("n");
        fresh.getName().length();
        Status[] all = Status.values();
    }

    public void inherited() {
        base(3);
        super.base(4);
        this.price(1);
        audit();
        tag();
        hashCode();
        rate = 2;
        int r = this.rate + super.rate;
    }

    public void external(Customer c) {
        history.add(null);
        history.stream().map(x -> x.cents()).forEach(y -> y.hashCode());
        List<Money> copy = new ArrayList<>(history);
        System.out.println(c);
        java.util.Collections.emptyList();
        String t = trim("  x ");
        long n = counting();
        Widget w = new Widget();
    }

    public void problems(Customer c) {
        c.undefined();
        unknownCall();
        Util.go();
        Thing.of();
        Account acc = new Account();
        acc.getId();
        Thing dup = new Thing();
        new Account("x");
        price(1, 2);
        int amb = price(unknownCall());
    }

    public void shadowing() {
        history.forEach(rate -> rate.hashCode());
        int k = rate;
    }

    public void blocks(boolean flag) {
        if (flag) {
            String v = "a";
            v.length();
        } else {
            Customer v = new Customer();
            v.getName();
        }
    }

    public void scoping(Customer owner) {
        owner.getName();
        int history = 0;
        history++;
        this.history.clear();
        this.owner.getName();
        List<Money> items = this.history;
        items.forEach(owner2 -> owner2.cents());
        Runnable r = new Runnable() {
            public void run() {
                base(1);
                owner.getName();
            }
        };
    }

    public void refs(List<Customer> customers) {
        customers.stream().map(Customer::getName);
        customers.forEach(Strings::trim);
        Runnable p = this::inherited;
        java.util.function.Function<Integer, Integer> f = this::price;
        java.util.function.Supplier<Customer> sup = Customer::new;
    }

    static <T extends Comparable<T>> T max(T a, T b) {
        return a.compareTo(b) > 0 ? a : b;
    }

    static int outerStatic(int v) {
        return v;
    }

    public static class Helper {
        int help(int x) {
            return LIMIT + x;
        }

        int up() {
            return help(1) + outerStatic(2);
        }
    }

    public int tryVar(Object o) {
        var created = new Customer();
        var text = "abc";
        var number = 42;
        created.getName();
        text.length();
        if (o instanceof Customer cust && cust.getStatus() != null) {
            return cust.getName().length();
        }
        try {
            return number;
        } catch (IllegalStateException | IllegalArgumentException ex) {
            return ex.getMessage().length();
        }
    }
}

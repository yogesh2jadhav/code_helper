package com.example.fixtures.types;

import java.io.Serializable;
import java.util.ArrayList;
import java.util.List;

@Deprecated
public abstract class TypeZoo<T extends Comparable<T>> extends Base implements Serializable, Runnable {
    public static final int LIMIT = 10;
    protected List<T> items = new ArrayList<>(), backup;
    private final String name;

    public TypeZoo(String name) {
        this.name = name;
    }

    @Override
    public void run() {
        Runnable r = new Runnable() {
            public void run() {
                System.out.println(name);
            }
        };
        r.run();
    }

    public abstract <R> R convert(T input, R... extras) throws java.io.IOException;

    static synchronized void helper() {}

    public interface Shape {
        double area();

        default String describe() {
            return "shape";
        }

        int SIDES = 4;
    }

    public enum Color implements Serializable {
        RED, GREEN, BLUE;

        public boolean isWarm() {
            return this == RED;
        }
    }

    public record Point(int x, int y) {
        public Point {
            if (x < 0) {
                throw new IllegalArgumentException();
            }
        }
    }

    public @interface Marker {}
}

class Base {}

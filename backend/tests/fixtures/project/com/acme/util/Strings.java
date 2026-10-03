package com.acme.util;

public final class Strings {
    private Strings() {}

    public static String trim(String s) {
        return s.trim();
    }

    public static int len(String s) {
        return s.length();
    }

    public static String join(String... parts) {
        return String.join(",", parts);
    }
}

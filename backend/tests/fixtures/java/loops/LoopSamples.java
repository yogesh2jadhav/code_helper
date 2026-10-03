package com.example.fixtures.loops;

import java.util.List;

public class LoopSamples {
    public int sumPositives(List<Integer> values) {
        int total = 0;
        for (int i = 0; i < values.size(); i++) {
            int v = values.get(i);
            if (v <= 0) {
                continue;
            }
            total += v;
        }
        return total;
    }

    public int firstNegativeIndex(int[] values) {
        int index = 0;
        while (index < values.length) {
            if (values[index] < 0) {
                break;
            }
            index++;
        }
        do {
            index--;
        } while (index > 100);
        for (int v : values) {
            index += v;
        }
        return index;
    }
}

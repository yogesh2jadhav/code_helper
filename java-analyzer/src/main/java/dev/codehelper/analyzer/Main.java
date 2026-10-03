package dev.codehelper.analyzer;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Reads absolute .java paths from stdin (one per line) and writes one compact JSON object per
 * file to stdout (NDJSON). A failure on one file never stops the batch.
 */
public final class Main {
    private Main() {}

    public static void main(String[] args) throws Exception {
        Gson gson = new GsonBuilder().disableHtmlEscaping().create();
        FileAnalyzer analyzer = new FileAnalyzer();
        PrintStream out = new PrintStream(System.out, false, StandardCharsets.UTF_8);
        try (BufferedReader in =
                new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = in.readLine()) != null) {
                String path = line.strip();
                if (path.isEmpty()) {
                    continue;
                }
                Model.FileResult result;
                try {
                    result = analyzer.analyze(java.nio.file.Path.of(path));
                } catch (Throwable t) {
                    result = new Model.FileResult(
                            path,
                            "analyzer_error",
                            List.of(t.getClass().getSimpleName() + ": " + FileAnalyzer.clip(t.getMessage(), 300)),
                            null,
                            List.of(),
                            List.of());
                }
                out.println(gson.toJson(result));
                out.flush();
            }
        }
    }
}

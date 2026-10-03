package dev.codehelper.analyzer;

import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.expr.AssignExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.ConditionalExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.LambdaExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.MethodReferenceExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.NullLiteralExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.SwitchExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.expr.VariableDeclarationExpr;
import com.github.javaparser.ast.stmt.BreakStmt;
import com.github.javaparser.ast.stmt.CatchClause;
import com.github.javaparser.ast.stmt.ContinueStmt;
import com.github.javaparser.ast.stmt.DoStmt;
import com.github.javaparser.ast.stmt.ForEachStmt;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.ast.stmt.ReturnStmt;
import com.github.javaparser.ast.stmt.SwitchEntry;
import com.github.javaparser.ast.stmt.SwitchStmt;
import com.github.javaparser.ast.stmt.ThrowStmt;
import com.github.javaparser.ast.stmt.TryStmt;
import com.github.javaparser.ast.stmt.WhileStmt;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Walks one method body and records statements and expressions as flat lists linked by ids.
 *
 * <p>Detection is purely syntactic (no symbol resolution): stream/Optional/collector tags are
 * derived from call-chain shape and well-known names, so they are heuristics, not type facts.
 *
 * <p>Statement depth = number of enclosing control structures (if/for/foreach/while/do/switch/try).
 * else-if chains stay at the depth of the first if; case/catch/else/finally entries sit at the same
 * depth as the statements in their bodies.
 */
final class MethodWalker {
    private static final Set<String> STREAM_SOURCES = Set.of("stream", "parallelStream");
    private static final Set<String> STREAM_TYPES = Set.of("Stream", "IntStream", "LongStream", "DoubleStream");
    private static final Set<String> STREAM_OPS = Set.of(
            "filter", "map", "flatMap", "mapToInt", "mapToLong", "mapToDouble", "mapToObj", "boxed",
            "distinct", "sorted", "limit", "skip", "peek", "collect", "reduce", "forEach", "forEachOrdered",
            "count", "min", "max", "sum", "average", "anyMatch", "allMatch", "noneMatch", "findFirst",
            "findAny", "toList", "toArray", "takeWhile", "dropWhile", "summaryStatistics");
    private static final Set<String> COLLECTOR_NAMES = Set.of(
            "toList", "toSet", "toMap", "toUnmodifiableList", "toUnmodifiableSet", "toUnmodifiableMap",
            "toCollection", "groupingBy", "groupingByConcurrent", "partitioningBy", "joining", "counting",
            "summingInt", "summingLong", "summingDouble", "averagingInt", "averagingLong", "averagingDouble",
            "minBy", "maxBy", "mapping", "filtering", "flatMapping", "reducing", "collectingAndThen",
            "summarizingInt", "summarizingLong", "summarizingDouble", "teeing");
    private static final Set<String> OPTIONAL_TERMINALS =
            Set.of("orElse", "orElseGet", "orElseThrow", "ifPresent", "ifPresentOrElse", "isPresent");
    private static final Set<String> OPTIONAL_CHAIN_OPS = Set.of("map", "flatMap", "filter", "or", "stream");
    private static final Set<String> PREDICATE_OPS =
            Set.of("filter", "anyMatch", "allMatch", "noneMatch", "removeIf", "takeWhile", "dropWhile");
    private static final Set<String> COMPARISON_CALLS = Set.of(
            "equals", "equalsIgnoreCase", "compareTo", "compareToIgnoreCase", "compare", "isAfter",
            "isBefore", "isEqual", "contentEquals");
    private static final Set<String> NULL_CHECK_CALLS = Set.of("isNull", "nonNull", "requireNonNull");
    private static final Set<String> COMPLEXITY_STATEMENTS =
            Set.of("if", "else_if", "for", "foreach", "while", "do", "case", "catch");

    private final boolean staticCollectors;
    private final List<Model.Statement> statements = new ArrayList<>();
    private final List<Model.Expression> expressions = new ArrayList<>();
    private final Set<Node> predicateArgs = Collections.newSetFromMap(new IdentityHashMap<>());
    private int nextStatementId = 1;
    private int nextExpressionId = 1;
    private int maxNesting = 0;

    MethodWalker(boolean staticCollectors) {
        this.staticCollectors = staticCollectors;
    }

    void walkBody(Node body) {
        walk(body, null, 0);
    }

    List<Model.Statement> statements() {
        return statements;
    }

    List<Model.Expression> expressions() {
        return expressions;
    }

    int maxNestingDepth() {
        return maxNesting;
    }

    int cyclomaticComplexity() {
        int n = 1;
        for (Model.Statement s : statements) {
            if (COMPLEXITY_STATEMENTS.contains(s.kind())) n++;
        }
        for (Model.Expression e : expressions) {
            if (e.kind().equals("ternary") || e.kind().equals("logical")) n++;
        }
        return n;
    }

    // ---- traversal ---------------------------------------------------------------------------

    /** Handles wrapper entries (plain else, finally) that have no node of their own, then visits. */
    private void walk(Node n, Integer parent, int depth) {
        Node p = n.getParentNode().orElse(null);
        if (p instanceof IfStmt ifs && !(n instanceof IfStmt) && ifs.getElseStmt().orElse(null) == n) {
            int id = addStatement("else", n, parent, depth, null);
            visit(n, id, depth);
        } else if (p instanceof TryStmt t && t.getFinallyBlock().orElse(null) == n) {
            int id = addStatement("finally", n, parent, depth, null);
            visit(n, id, depth);
        } else {
            visit(n, parent, depth);
        }
    }

    private void visit(Node n, Integer parent, int depth) {
        Integer childParent = parent;
        int childDepth = depth;

        if (n instanceof IfStmt s) {
            boolean elseIf = n.getParentNode().filter(p -> p instanceof IfStmt ps
                    && ps.getElseStmt().orElse(null) == n).isPresent();
            int id = addStatement(elseIf ? "else_if" : "if", n, parent, depth, clipExpr(s.getCondition()));
            control(depth);
            for (Node c : n.getChildNodes()) {
                boolean isElseIf = c instanceof IfStmt && s.getElseStmt().orElse(null) == c;
                walk(c, id, isElseIf ? depth : depth + 1);
            }
            return;
        } else if (n instanceof ForStmt s) {
            String header = s.getInitialization().stream().map(Node::toString).collect(Collectors.joining(", "))
                    + "; " + s.getCompare().map(Node::toString).orElse("") + "; "
                    + s.getUpdate().stream().map(Node::toString).collect(Collectors.joining(", "));
            childParent = addStatement("for", n, parent, depth, FileAnalyzer.clip(FileAnalyzer.oneLine(header), 200));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof ForEachStmt s) {
            childParent = addStatement("foreach", n, parent, depth, FileAnalyzer.clip(
                    FileAnalyzer.oneLine(s.getVariable() + " : " + s.getIterable()), 200));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof WhileStmt s) {
            childParent = addStatement("while", n, parent, depth, clipExpr(s.getCondition()));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof DoStmt s) {
            childParent = addStatement("do", n, parent, depth, clipExpr(s.getCondition()));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof SwitchStmt s) {
            childParent = addStatement("switch", n, parent, depth, clipExpr(s.getSelector()));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof TryStmt) {
            childParent = addStatement("try", n, parent, depth, null);
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof SwitchEntry e) {
            String labels = e.getLabels().stream().map(Node::toString).collect(Collectors.joining(", "));
            childParent = addStatement(e.getLabels().isEmpty() ? "default" : "case", n, parent, depth,
                    labels.isEmpty() ? null : FileAnalyzer.clip(FileAnalyzer.oneLine(labels), 200));
        } else if (n instanceof CatchClause c) {
            childParent = addStatement("catch", n, parent, depth,
                    FileAnalyzer.clip(FileAnalyzer.oneLine(c.getParameter().toString()), 200));
        } else if (n instanceof ReturnStmt s) {
            childParent = addStatement("return", n, parent, depth, s.getExpression().map(MethodWalker::clipExpr).orElse(null));
        } else if (n instanceof ThrowStmt s) {
            childParent = addStatement("throw", n, parent, depth, clipExpr(s.getExpression()));
        } else if (n instanceof BreakStmt s) {
            childParent = addStatement("break", n, parent, depth, s.getLabel().map(Object::toString).orElse(null));
        } else if (n instanceof ContinueStmt s) {
            childParent = addStatement("continue", n, parent, depth, s.getLabel().map(Object::toString).orElse(null));
        } else if (n instanceof Expression e) {
            if (n instanceof SwitchExpr) {
                control(depth);
                childDepth = depth + 1;
            }
            recordExpression(e, parent);
        }

        for (Node c : n.getChildNodes()) {
            walk(c, childParent, childDepth);
        }
    }

    private void control(int depth) {
        maxNesting = Math.max(maxNesting, depth + 1);
    }

    // ---- statements --------------------------------------------------------------------------

    private int addStatement(String kind, Node n, Integer parent, int depth, String text) {
        int id = nextStatementId++;
        statements.add(new Model.Statement(id, parent, kind, line(n, true), line(n, false), depth, text));
        return id;
    }

    // ---- expressions -------------------------------------------------------------------------

    private void recordExpression(Expression e, Integer stmt) {
        if (e instanceof MethodCallExpr c) {
            methodCall(c, stmt);
        } else if (e instanceof FieldAccessExpr f) {
            add("field_access", e, stmt, f.getNameAsString(), f.getScope().toString(), null, null, null, List.of());
        } else if (e instanceof AssignExpr a) {
            add("assignment", e, stmt, FileAnalyzer.clip(FileAnalyzer.oneLine(a.getTarget().toString()), 120),
                    null, null, null, a.getOperator().asString(), List.of());
        } else if (e instanceof UnaryExpr u && isIncDec(u)) {
            add("assignment", e, stmt, FileAnalyzer.clip(FileAnalyzer.oneLine(u.getExpression().toString()), 120),
                    null, null, null, incDecSymbol(u), List.of());
        } else if (e instanceof ObjectCreationExpr o) {
            add("object_creation", e, stmt, o.getType().getNameAsString(), null, o.getType().asString(),
                    o.getArguments().size(), null, o.getAnonymousClassBody().isPresent() ? List.of("anonymous_class") : List.of());
        } else if (e instanceof LambdaExpr l) {
            add("lambda", e, stmt, null, null, null, l.getParameters().size(), null,
                    predicateArgs.contains(e) ? List.of("predicate") : List.of());
        } else if (e instanceof MethodReferenceExpr r) {
            List<String> tags = new ArrayList<>();
            if (predicateArgs.contains(e)) tags.add("predicate");
            if (r.getScope().toString().equals("Objects") && NULL_CHECK_CALLS.contains(r.getIdentifier())) {
                tags.add("null_check");
            }
            add("method_ref", e, stmt, r.getIdentifier(), r.getScope().toString(), null, null, null, tags);
        } else if (e instanceof SwitchExpr) {
            add("switch_expr", e, stmt, null, null, null, null, null, List.of());
        } else if (e instanceof ConditionalExpr) {
            add("ternary", e, stmt, null, null, null, null, "?:", List.of());
        } else if (e instanceof BinaryExpr b) {
            binary(b, stmt);
        } else if (e instanceof VariableDeclarationExpr v) {
            v.getVariables().forEach(d -> add("variable_declaration", d, stmt, d.getNameAsString(), null,
                    d.getType().asString(), null, null, d.getInitializer().isPresent() ? List.of("initialized") : List.of()));
        }
    }

    private void binary(BinaryExpr b, Integer stmt) {
        BinaryExpr.Operator op = b.getOperator();
        switch (op) {
            case EQUALS, NOT_EQUALS -> {
                boolean nullSide = b.getLeft() instanceof NullLiteralExpr || b.getRight() instanceof NullLiteralExpr;
                add(nullSide ? "null_check" : "comparison", b, stmt, null, null, null, null, op.asString(), List.of());
            }
            case LESS, GREATER, LESS_EQUALS, GREATER_EQUALS ->
                add("comparison", b, stmt, null, null, null, null, op.asString(), List.of());
            case AND, OR -> add("logical", b, stmt, null, null, null, null, op.asString(), List.of());
            default -> { }
        }
    }

    private void methodCall(MethodCallExpr c, Integer stmt) {
        String name = c.getNameAsString();
        String scopeText = c.getScope().map(Node::toString).orElse(null);
        String scopeRoot = c.getScope().filter(s -> s instanceof NameExpr).map(s -> ((NameExpr) s).getNameAsString()).orElse(null);
        String chain = chainRoot(c);
        List<String> tags = new ArrayList<>();

        if (STREAM_SOURCES.contains(name) || (scopeRoot != null && STREAM_TYPES.contains(scopeRoot))) {
            tags.add("stream_source");
        } else if ("stream".equals(chain) && STREAM_OPS.contains(name)) {
            tags.add("stream_op");
        }
        if ("Collectors".equals(scopeRoot) && COLLECTOR_NAMES.contains(name)
                || (scopeText == null && staticCollectors && COLLECTOR_NAMES.contains(name))) {
            tags.add("collector");
        }
        if ("Optional".equals(scopeRoot) || OPTIONAL_TERMINALS.contains(name)
                || ("optional".equals(chain) && OPTIONAL_CHAIN_OPS.contains(name))) {
            tags.add("optional");
        }
        if (COMPARISON_CALLS.contains(name)) tags.add("comparison");
        if ("Objects".equals(scopeRoot) && NULL_CHECK_CALLS.contains(name)) tags.add("null_check");

        boolean predicateOwner = PREDICATE_OPS.contains(name) && (chain != null || !name.equals("filter"))
                || ("Collectors".equals(scopeRoot) && (name.equals("partitioningBy") || name.equals("filtering")))
                || ("Predicate".equals(scopeRoot) && name.equals("not"));
        if (predicateOwner) {
            c.getArguments().stream()
                    .filter(a -> a instanceof LambdaExpr || a instanceof MethodReferenceExpr)
                    .findFirst()
                    .ifPresent(predicateArgs::add);
        }
        add("method_call", c, stmt, name, scopeText == null ? null : FileAnalyzer.clip(FileAnalyzer.oneLine(scopeText), 120),
                null, c.getArguments().size(), null, tags);
    }

    /** "stream" / "optional" if the receiver chain starts from a recognisable source, else null. */
    private static String chainRoot(MethodCallExpr call) {
        Expression scope = call.getScope().orElse(null);
        while (scope instanceof MethodCallExpr m) {
            if (STREAM_SOURCES.contains(m.getNameAsString())) return "stream";
            if (m.getScope().orElse(null) instanceof NameExpr q) {
                String root = q.getNameAsString();
                if (STREAM_TYPES.contains(root)) return "stream";
                if (root.equals("Optional")) return "optional";
            }
            scope = m.getScope().orElse(null);
        }
        return null;
    }

    private void add(String kind, Node n, Integer stmt, String name, String scope, String type,
                     Integer argCount, String operator, List<String> tags) {
        expressions.add(new Model.Expression(nextExpressionId++, stmt, kind, line(n, true), line(n, false),
                clipExpr(n), name, scope, type, argCount, operator, tags));
    }

    private static boolean isIncDec(UnaryExpr u) {
        return switch (u.getOperator()) {
            case PREFIX_INCREMENT, PREFIX_DECREMENT, POSTFIX_INCREMENT, POSTFIX_DECREMENT -> true;
            default -> false;
        };
    }

    private static String incDecSymbol(UnaryExpr u) {
        return switch (u.getOperator()) {
            case PREFIX_INCREMENT, POSTFIX_INCREMENT -> "++";
            default -> "--";
        };
    }

    private static String clipExpr(Node n) {
        return FileAnalyzer.clip(FileAnalyzer.oneLine(n.toString()), 200);
    }

    private static int line(Node n, boolean begin) {
        return n.getRange().map(r -> begin ? r.begin.line : r.end.line).orElse(-1);
    }
}

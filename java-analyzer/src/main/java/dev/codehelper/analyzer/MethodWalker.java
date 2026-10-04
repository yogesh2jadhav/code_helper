package dev.codehelper.analyzer;

import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.expr.AssignExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.BooleanLiteralExpr;
import com.github.javaparser.ast.expr.CastExpr;
import com.github.javaparser.ast.expr.CharLiteralExpr;
import com.github.javaparser.ast.expr.ClassExpr;
import com.github.javaparser.ast.expr.ConditionalExpr;
import com.github.javaparser.ast.expr.DoubleLiteralExpr;
import com.github.javaparser.ast.expr.EnclosedExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.InstanceOfExpr;
import com.github.javaparser.ast.expr.IntegerLiteralExpr;
import com.github.javaparser.ast.expr.LambdaExpr;
import com.github.javaparser.ast.expr.LongLiteralExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.MethodReferenceExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.NullLiteralExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.PatternExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.expr.SuperExpr;
import com.github.javaparser.ast.expr.SwitchExpr;
import com.github.javaparser.ast.expr.TextBlockLiteralExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.expr.VariableDeclarationExpr;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.stmt.BreakStmt;
import com.github.javaparser.ast.stmt.CatchClause;
import com.github.javaparser.ast.stmt.ContinueStmt;
import com.github.javaparser.ast.stmt.DoStmt;
import com.github.javaparser.ast.stmt.ForEachStmt;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.ast.stmt.LocalClassDeclarationStmt;
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
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Walks one method body and records statements and expressions as flat lists linked by ids.
 *
 * <p>Detection is purely syntactic (no symbol resolution): stream/Optional/collector tags are
 * derived from call-chain shape and well-known names, so they are heuristics, not type facts.
 * Name resolution happens later, in Python, over these facts.
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

    /** Mutable expression record, frozen into {@link Model.Expression} once receivers are linked. */
    private static final class Ex {
        int id;
        Integer statementId;
        String kind;
        int startLine;
        int endLine;
        String text;
        String name;
        String scope;
        String type;
        Integer argCount;
        String operator;
        final List<String> tags = new ArrayList<>();
        String receiverKind;
        Node receiverNode;
        Integer receiverExprId;
        String receiverType;
        List<Model.ValueHint> args;
        Model.ValueHint initializer;
        Integer scopeEndLine;
        int startColumn;
        int endColumn;
        String left;
        String right;
        List<String> argTexts;

        Model.Expression freeze() {
            return new Model.Expression(id, statementId, kind, startLine, endLine, text, name, scope, type,
                    argCount, operator, tags, receiverKind, receiverExprId, receiverType, args, initializer,
                    scopeEndLine, startColumn, endColumn, left, right, argTexts);
        }
    }

    private final boolean staticCollectors;
    private final List<Model.Statement> statements = new ArrayList<>();
    private final List<Ex> exprs = new ArrayList<>();
    private final Map<Node, Integer> exprIds = new IdentityHashMap<>();
    private final Set<Node> predicateArgs = Collections.newSetFromMap(new IdentityHashMap<>());
    private final List<PendingHint> pendingHints = new ArrayList<>();

    /** An "expr" hint waiting for the id of the expression it points at (index -1 = initializer). */
    private record PendingHint(Ex owner, int index, Node target) {}
    private int nextStatementId = 1;
    private int nextExpressionId = 1;
    private int maxNesting = 0;
    private int localClassDepth = 0;

    MethodWalker(boolean staticCollectors) {
        this.staticCollectors = staticCollectors;
    }

    void walkBody(Node body) {
        walk(body, null, 0);
        for (PendingHint p : pendingHints) {
            Integer id = exprIds.get(p.target());
            Model.ValueHint current = p.index() >= 0 ? p.owner().args.get(p.index()) : p.owner().initializer;
            Model.ValueHint linked = id == null
                    ? new Model.ValueHint("other", null, null, null, current.range())
                    : current.withExprId(id);
            if (p.index() >= 0) {
                p.owner().args.set(p.index(), linked);
            } else {
                p.owner().initializer = linked;
            }
        }
        for (Ex ex : exprs) {
            if (ex.receiverNode != null) {
                Integer id = exprIds.get(ex.receiverNode);
                if (id != null) {
                    ex.receiverExprId = id;
                } else {
                    ex.receiverKind = "other";
                }
            }
        }
    }

    List<Model.Statement> statements() {
        return statements;
    }

    List<Model.Expression> expressions() {
        return exprs.stream().map(Ex::freeze).collect(Collectors.toList());
    }

    int maxNestingDepth() {
        return maxNesting;
    }

    int cyclomaticComplexity() {
        int n = 1;
        for (Model.Statement s : statements) {
            if (COMPLEXITY_STATEMENTS.contains(s.kind())) n++;
        }
        for (Ex e : exprs) {
            if (e.kind.equals("ternary") || e.kind.equals("logical")) n++;
        }
        return n;
    }

    // ---- traversal ---------------------------------------------------------------------------

    /** Handles wrapper entries (plain else, finally) that have no node of their own, then visits. */
    private void walk(Node n, Integer parent, int depth) {
        boolean enteringLocalClass = n instanceof LocalClassDeclarationStmt || isAnonymousBodyMember(n);
        if (enteringLocalClass) localClassDepth++;
        try {
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
        } finally {
            if (enteringLocalClass) localClassDepth--;
        }
    }

    private static boolean isAnonymousBodyMember(Node n) {
        if (!(n instanceof BodyDeclaration<?>)) return false;
        Node p = n.getParentNode().orElse(null);
        if (!(p instanceof ObjectCreationExpr oce)) return false;
        return oce.getAnonymousClassBody().map(body -> containsIdentity(body, n)).orElse(false);
    }

    private static boolean containsIdentity(List<?> list, Object o) {
        for (Object x : list) {
            if (x == o) return true;
        }
        return false;
    }

    private void visit(Node n, Integer parent, int depth) {
        Integer childParent = parent;
        int childDepth = depth;

        if (n instanceof IfStmt s) {
            boolean elseIf = n.getParentNode().filter(p -> p instanceof IfStmt ps
                    && ps.getElseStmt().orElse(null) == n).isPresent();
            int id = addStatement(elseIf ? "else_if" : "if", n, parent, depth, clipExpr(s.getCondition()),
                    rangeOf(s.getCondition()));
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
            List<Node> headerNodes = new ArrayList<>(s.getInitialization());
            s.getCompare().ifPresent(headerNodes::add);
            headerNodes.addAll(s.getUpdate());
            childParent = addStatement("for", n, parent, depth, FileAnalyzer.clip(FileAnalyzer.oneLine(header), 200),
                    unionRange(headerNodes));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof ForEachStmt s) {
            childParent = addStatement("foreach", n, parent, depth, FileAnalyzer.clip(
                    FileAnalyzer.oneLine(s.getVariable() + " : " + s.getIterable()), 200),
                    unionRange(List.of(s.getVariable(), s.getIterable())));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof WhileStmt s) {
            childParent = addStatement("while", n, parent, depth, clipExpr(s.getCondition()),
                    rangeOf(s.getCondition()));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof DoStmt s) {
            childParent = addStatement("do", n, parent, depth, clipExpr(s.getCondition()),
                    rangeOf(s.getCondition()));
            control(depth);
            childDepth = depth + 1;
        } else if (n instanceof SwitchStmt s) {
            childParent = addStatement("switch", n, parent, depth, clipExpr(s.getSelector()),
                    rangeOf(s.getSelector()));
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
                    FileAnalyzer.clip(FileAnalyzer.oneLine(c.getParameter().toString()), 200),
                    rangeOf(c.getParameter()));
            Ex ex = add("variable_declaration", c.getParameter(), childParent, c.getParameter().getNameAsString(),
                    null, c.getParameter().getType().asString(), null, null, List.of("catch_param"));
            ex.scopeEndLine = line(c, false);
        } else if (n instanceof ReturnStmt s) {
            childParent = addStatement("return", n, parent, depth,
                    s.getExpression().map(MethodWalker::clipExpr).orElse(null),
                    s.getExpression().map(MethodWalker::rangeOf).orElse(null));
        } else if (n instanceof ThrowStmt s) {
            childParent = addStatement("throw", n, parent, depth, clipExpr(s.getExpression()),
                    rangeOf(s.getExpression()));
        } else if (n instanceof BreakStmt s) {
            childParent = addStatement("break", n, parent, depth, s.getLabel().map(Object::toString).orElse(null));
        } else if (n instanceof ContinueStmt s) {
            childParent = addStatement("continue", n, parent, depth, s.getLabel().map(Object::toString).orElse(null));
        } else if (n instanceof com.github.javaparser.ast.body.Parameter p
                && p.getParentNode().orElse(null) instanceof CallableDeclaration<?> owner) {
            // a parameter of a method that lives inside an anonymous/local class
            Ex ex = add("variable_declaration", p, parent, p.getNameAsString(), null, p.getType().asString(),
                    null, null, List.of("param"));
            ex.scopeEndLine = line(owner, false);
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
        return addStatement(kind, n, parent, depth, text, null);
    }

    private int addStatement(String kind, Node n, Integer parent, int depth, String text, Model.Range value) {
        int id = nextStatementId++;
        statements.add(new Model.Statement(id, parent, kind, line(n, true), line(n, false), depth, text,
                column(n, true), column(n, false), value));
        return id;
    }

    // ---- expressions -------------------------------------------------------------------------

    private void recordExpression(Expression e, Integer stmt) {
        if (e instanceof MethodCallExpr c) {
            methodCall(c, stmt);
        } else if (e instanceof NameExpr ne) {
            nameRef(ne, stmt);
        } else if (e instanceof FieldAccessExpr f) {
            Ex ex = add("field_access", e, stmt, f.getNameAsString(), f.getScope().toString(), null, null, null, List.of());
            setReceiver(ex, f.getScope());
        } else if (e instanceof AssignExpr a) {
            Ex ex = add("assignment", e, stmt, FileAnalyzer.clip(FileAnalyzer.oneLine(a.getTarget().toString()), 120),
                    null, null, null, a.getOperator().asString(), List.of());
            ex.left = clipExpr(a.getTarget());
            ex.right = clipExpr(a.getValue());
            ex.initializer = hint(a.getValue());
            if (ex.initializer.kind().equals("expr")) {
                pendingHints.add(new PendingHint(ex, -1, unwrap(a.getValue())));
            }
        } else if (e instanceof UnaryExpr u && isIncDec(u)) {
            add("assignment", e, stmt, FileAnalyzer.clip(FileAnalyzer.oneLine(u.getExpression().toString()), 120),
                    null, null, null, incDecSymbol(u), List.of());
        } else if (e instanceof ObjectCreationExpr o) {
            Ex ex = add("object_creation", e, stmt, o.getType().getNameAsString(), null, o.getType().asString(),
                    o.getArguments().size(), null,
                    o.getAnonymousClassBody().isPresent() ? List.of("anonymous_class") : List.of());
            setArgs(ex, o.getArguments());
            ex.argTexts = o.getArguments().stream().map(MethodWalker::clipArg).collect(Collectors.toList());
        } else if (e instanceof LambdaExpr l) {
            add("lambda", e, stmt, null, null, null, l.getParameters().size(), null,
                    predicateArgs.contains(e) ? List.of("predicate") : List.of());
            for (com.github.javaparser.ast.body.Parameter p : l.getParameters()) {
                Ex ex = add("variable_declaration", p, stmt, p.getNameAsString(), null,
                        p.getType().isUnknownType() ? null : p.getType().asString(), null, null, List.of("lambda_param"));
                ex.scopeEndLine = line(l, false);
            }
        } else if (e instanceof MethodReferenceExpr r) {
            List<String> tags = new ArrayList<>();
            if (predicateArgs.contains(e)) tags.add("predicate");
            if (r.getScope().toString().equals("Objects") && NULL_CHECK_CALLS.contains(r.getIdentifier())) {
                tags.add("null_check");
            }
            Ex ex = add("method_ref", e, stmt, r.getIdentifier(), r.getScope().toString(), null, null, null, tags);
            setReceiver(ex, r.getScope());
        } else if (e instanceof ConditionalExpr) {
            add("ternary", e, stmt, null, null, null, null, "?:", List.of());
        } else if (e instanceof BinaryExpr b) {
            binary(b, stmt);
        } else if (e instanceof SwitchExpr) {
            add("switch_expr", e, stmt, null, null, null, null, null, List.of());
        } else if (e instanceof VariableDeclarationExpr v) {
            for (com.github.javaparser.ast.body.VariableDeclarator d : v.getVariables()) {
                List<String> tags = new ArrayList<>();
                if (d.getInitializer().isPresent()) tags.add("initialized");
                Node holder = v.getParentNode().orElse(null);
                if (holder instanceof ForEachStmt) tags.add("foreach");
                else if (holder instanceof ForStmt) tags.add("for_init");
                else if (holder instanceof TryStmt) tags.add("resource");
                Ex ex = add("variable_declaration", d, stmt, d.getNameAsString(), null, d.getType().asString(),
                        null, null, tags);
                ex.scopeEndLine = declarationScopeEnd(v);
                d.getInitializer().ifPresent(init -> ex.right = clipExpr(init));
                d.getInitializer().ifPresent(init -> {
                    ex.initializer = hint(init);
                    if (ex.initializer.kind().equals("expr")) {
                        pendingHints.add(new PendingHint(ex, -1, unwrap(init)));
                    }
                });
            }
        } else if (e instanceof InstanceOfExpr io) {
            Optional<PatternExpr> pattern = io.getPattern();
            if (pattern.isPresent() && pattern.get().isTypePatternExpr()) {
                var tp = pattern.get().asTypePatternExpr();
                Ex ex = add("variable_declaration", tp, stmt, tp.getNameAsString(), null, tp.getType().asString(),
                        null, null, List.of("pattern"));
                ex.scopeEndLine = enclosingBlockEnd(io);
            }
        }
    }

    private void nameRef(NameExpr ne, Integer stmt) {
        Node p = ne.getParentNode().orElse(null);
        if (p instanceof SwitchEntry se && containsIdentity(se.getLabels(), ne)) {
            return; // enum-constant case label, not a variable reference
        }
        List<String> tags = new ArrayList<>();
        if ((p instanceof AssignExpr a && a.getTarget() == ne)
                || (p instanceof UnaryExpr u && isIncDec(u) && u.getExpression() == ne)) {
            tags.add("assignment_target");
        }
        add("name_ref", ne, stmt, ne.getNameAsString(), null, null, null, null, tags);
    }

    private void binary(BinaryExpr b, Integer stmt) {
        BinaryExpr.Operator op = b.getOperator();
        String kind;
        switch (op) {
            case EQUALS, NOT_EQUALS -> {
                boolean nullSide = b.getLeft() instanceof NullLiteralExpr || b.getRight() instanceof NullLiteralExpr;
                kind = nullSide ? "null_check" : "comparison";
            }
            case LESS, GREATER, LESS_EQUALS, GREATER_EQUALS -> kind = "comparison";
            case AND, OR -> kind = "logical";
            default -> {
                return;
            }
        }
        Ex ex = add(kind, b, stmt, null, null, null, null, op.asString(), List.of());
        ex.left = clipExpr(b.getLeft());
        ex.right = clipExpr(b.getRight());
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
        Ex ex = add("method_call", c, stmt, name,
                scopeText == null ? null : FileAnalyzer.clip(FileAnalyzer.oneLine(scopeText), 120),
                null, c.getArguments().size(), null, tags);
        setReceiver(ex, c.getScope().orElse(null));
        setArgs(ex, c.getArguments());
        ex.argTexts = c.getArguments().stream().map(MethodWalker::clipArg).collect(Collectors.toList());
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

    // ---- receivers, hints, scopes -------------------------------------------------------------

    private void setReceiver(Ex ex, Expression scope) {
        if (scope == null) {
            ex.receiverKind = "none";
            return;
        }
        Expression s = unwrap(scope);
        if (s instanceof ThisExpr t) {
            ex.receiverKind = t.getTypeName().isPresent() ? "other" : "this";
        } else if (s instanceof SuperExpr) {
            ex.receiverKind = "super";
        } else if (s instanceof NameExpr || s instanceof MethodCallExpr || s instanceof FieldAccessExpr
                || s instanceof ObjectCreationExpr) {
            ex.receiverKind = "expr";
            ex.receiverNode = s; // linked to an expression id once the whole body is walked
        } else if (s instanceof CastExpr c) {
            ex.receiverKind = "cast";
            ex.receiverType = c.getType().asString();
        } else {
            Model.ValueHint h = hint(s);
            if (h.kind().equals("literal")) {
                ex.receiverKind = "literal";
                ex.receiverType = h.type();
            } else {
                ex.receiverKind = "other";
            }
        }
    }

    private static Expression unwrap(Expression e) {
        while (e instanceof EnclosedExpr en) e = en.getInner();
        return e;
    }

    private void setArgs(Ex ex, List<Expression> args) {
        List<Model.ValueHint> out = new ArrayList<>();
        for (int i = 0; i < args.size(); i++) {
            Model.ValueHint h = hint(args.get(i));
            out.add(h);
            if (h.kind().equals("expr")) {
                pendingHints.add(new PendingHint(ex, i, unwrap(args.get(i))));
            }
        }
        ex.args = out;
    }

    private static Model.ValueHint hint(Expression raw) {
        return plainHint(raw).withRange(rangeOf(raw));
    }

    private static Model.ValueHint plainHint(Expression raw) {
        Expression e = unwrap(raw);
        if (e instanceof NullLiteralExpr) return new Model.ValueHint("literal", "null", null);
        if (e instanceof StringLiteralExpr || e instanceof TextBlockLiteralExpr) return new Model.ValueHint("literal", "String", null);
        if (e instanceof IntegerLiteralExpr) return new Model.ValueHint("literal", "int", null);
        if (e instanceof LongLiteralExpr) return new Model.ValueHint("literal", "long", null);
        if (e instanceof CharLiteralExpr) return new Model.ValueHint("literal", "char", null);
        if (e instanceof BooleanLiteralExpr) return new Model.ValueHint("literal", "boolean", null);
        if (e instanceof DoubleLiteralExpr d) {
            String v = d.getValue();
            boolean isFloat = v.endsWith("f") || v.endsWith("F");
            return new Model.ValueHint("literal", isFloat ? "float" : "double", null);
        }
        if (e instanceof UnaryExpr u && (u.getOperator() == UnaryExpr.Operator.MINUS
                || u.getOperator() == UnaryExpr.Operator.PLUS)) {
            Model.ValueHint inner = plainHint(u.getExpression());
            if (inner.kind().equals("literal") && !inner.type().equals("String") && !inner.type().equals("null")
                    && !inner.type().equals("boolean") && !inner.type().equals("char")) {
                return inner;
            }
            return new Model.ValueHint("other", null, null);
        }
        if (e instanceof NameExpr n) return new Model.ValueHint("name", null, n.getNameAsString());
        if (e instanceof ObjectCreationExpr o) return new Model.ValueHint("new", o.getType().asString(), null);
        if (e instanceof CastExpr c) return new Model.ValueHint("cast", c.getType().asString(), null);
        if (e instanceof ThisExpr t && t.getTypeName().isEmpty()) return new Model.ValueHint("this", null, null);
        if (e instanceof ClassExpr) return new Model.ValueHint("literal", "Class", null);
        if (e instanceof MethodCallExpr || e instanceof FieldAccessExpr) {
            return new Model.ValueHint("expr", null, null); // linked to an expression id later
        }
        return new Model.ValueHint("other", null, null);
    }

    /** Last line on which a local variable declared by `decl` is visible. */
    private static int declarationScopeEnd(VariableDeclarationExpr decl) {
        Node parent = decl.getParentNode().orElse(null);
        if (parent instanceof ForEachStmt || parent instanceof ForStmt || parent instanceof TryStmt) {
            return line(parent, false);
        }
        return enclosingBlockEnd(decl);
    }

    private static int enclosingBlockEnd(Node from) {
        Node n = from.getParentNode().orElse(null);
        while (n != null) {
            if (n instanceof BlockStmt) return line(n, false);
            if (n instanceof SwitchEntry se) {
                // a declaration in an old-style case group is visible to the rest of the switch block
                return se.getParentNode().map(sw -> line(sw, false)).orElse(line(se, false));
            }
            if (n instanceof CallableDeclaration<?> || n instanceof LambdaExpr) return line(n, false);
            n = n.getParentNode().orElse(null);
        }
        return line(from, false);
    }

    // ---- builders ----------------------------------------------------------------------------

    private Ex add(String kind, Node n, Integer stmt, String name, String scope, String type,
                   Integer argCount, String operator, List<String> tags) {
        Ex ex = new Ex();
        ex.id = nextExpressionId++;
        ex.statementId = stmt;
        ex.kind = kind;
        ex.startLine = line(n, true);
        ex.endLine = line(n, false);
        ex.startColumn = column(n, true);
        ex.endColumn = column(n, false);
        ex.text = clipExpr(n);
        ex.name = name;
        ex.scope = scope;
        ex.type = type;
        ex.argCount = argCount;
        ex.operator = operator;
        ex.tags.addAll(tags);
        if (localClassDepth > 0) ex.tags.add("in_local_class");
        exprs.add(ex);
        exprIds.put(n, ex.id);
        return ex;
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

    private static String clipArg(Expression e) {
        return FileAnalyzer.clip(FileAnalyzer.oneLine(e.toString()), 160);
    }

    private static Model.Range unionRange(List<? extends Node> nodes) {
        Model.Range out = null;
        for (Node n : nodes) {
            Model.Range r = rangeOf(n);
            if (r == null) continue;
            if (out == null) {
                out = r;
            } else {
                boolean earlier = r.startLine() < out.startLine()
                        || (r.startLine() == out.startLine() && r.startColumn() < out.startColumn());
                boolean later = r.endLine() > out.endLine()
                        || (r.endLine() == out.endLine() && r.endColumn() > out.endColumn());
                out = new Model.Range(
                        earlier ? r.startLine() : out.startLine(), earlier ? r.startColumn() : out.startColumn(),
                        later ? r.endLine() : out.endLine(), later ? r.endColumn() : out.endColumn());
            }
        }
        return out;
    }

    private static Model.Range rangeOf(Node n) {
        return n.getRange()
                .map(r -> new Model.Range(r.begin.line, r.begin.column, r.end.line, r.end.column))
                .orElse(null);
    }

    private static int column(Node n, boolean begin) {
        return n.getRange().map(r -> begin ? r.begin.column : r.end.column).orElse(-1);
    }

    private static int line(Node n, boolean begin) {
        return n.getRange().map(r -> begin ? r.begin.line : r.end.line).orElse(-1);
    }
}

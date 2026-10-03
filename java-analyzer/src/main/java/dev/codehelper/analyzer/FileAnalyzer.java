package dev.codehelper.analyzer;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Modifier;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.AnnotationDeclaration;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.CallableDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.CompactConstructorDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.EnumConstantDeclaration;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.RecordDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.type.TypeParameter;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/** Parses one Java file and extracts class/method structure. No symbol resolution happens here. */
final class FileAnalyzer {
    private final JavaParser parser;

    FileAnalyzer() {
        ParserConfiguration config = new ParserConfiguration();
        config.setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_17);
        config.setAttributeComments(true);
        this.parser = new JavaParser(config);
    }

    Model.FileResult analyze(Path path) throws IOException {
        String source = decode(Files.readAllBytes(path));
        String[] lines = source.split("\\R", -1);
        ParseResult<CompilationUnit> result = parser.parse(source);

        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            List<String> errors = new ArrayList<>();
            result.getProblems().forEach(p -> errors.add(clip(p.getMessage(), 500)));
            return new Model.FileResult(path.toString(), "parse_error", errors, null, List.of(), List.of());
        }

        CompilationUnit cu = result.getResult().get();
        String pkg = cu.getPackageDeclaration().map(p -> p.getNameAsString()).orElse(null);

        List<Model.Import> imports = new ArrayList<>();
        cu.getImports().forEach(i -> imports.add(new Model.Import(i.getNameAsString(), i.isStatic(), i.isAsterisk())));
        boolean staticCollectors = cu.getImports().stream()
                .anyMatch(i -> i.isStatic() && i.getNameAsString().startsWith("java.util.stream.Collectors"));

        List<Model.TypeDecl> types = new ArrayList<>();
        for (TypeDeclaration<?> td : cu.getTypes()) {
            types.add(type(td, pkg, null, lines, staticCollectors));
        }
        return new Model.FileResult(path.toString(), "ok", List.of(), pkg, imports, types);
    }

    /** Strict UTF-8, falling back to Latin-1 so legacy files still parse. */
    private static String decode(byte[] bytes) {
        try {
            return StandardCharsets.UTF_8
                    .newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes))
                    .toString();
        } catch (CharacterCodingException e) {
            return new String(bytes, StandardCharsets.ISO_8859_1);
        }
    }

    // ---- types -------------------------------------------------------------------------------

    private Model.TypeDecl type(
            TypeDeclaration<?> td, String pkg, String outer, // outer = enclosing type's qualified name
            String[] lines, boolean staticCollectors) {
        String kind;
        String superclass = null;
        List<String> interfaces = new ArrayList<>();
        List<String> typeParams = new ArrayList<>();
        List<String> enumConstants = new ArrayList<>();
        List<Model.Parameter> components = new ArrayList<>();
        boolean isInterface = false;

        if (td instanceof ClassOrInterfaceDeclaration c) {
            isInterface = c.isInterface();
            kind = isInterface ? "interface" : "class";
            if (isInterface) {
                c.getExtendedTypes().forEach(t -> interfaces.add(t.asString()));
            } else {
                superclass = c.getExtendedTypes().isEmpty() ? null : c.getExtendedTypes().get(0).asString();
            }
            c.getImplementedTypes().forEach(t -> interfaces.add(t.asString()));
            c.getTypeParameters().forEach(t -> typeParams.add(t.asString()));
        } else if (td instanceof EnumDeclaration e) {
            kind = "enum";
            e.getImplementedTypes().forEach(t -> interfaces.add(t.asString()));
            for (EnumConstantDeclaration k : e.getEntries()) {
                enumConstants.add(k.getNameAsString());
            }
        } else if (td instanceof RecordDeclaration r) {
            kind = "record";
            r.getImplementedTypes().forEach(t -> interfaces.add(t.asString()));
            r.getTypeParameters().forEach(t -> typeParams.add(t.asString()));
            r.getParameters().forEach(p -> components.add(parameter(p)));
        } else if (td instanceof AnnotationDeclaration) {
            kind = "annotation";
        } else {
            kind = "unknown";
        }

        String name = td.getNameAsString();
        String qualifiedName = outer != null ? outer + "." + name : pkg != null ? pkg + "." + name : name;

        List<Model.Field> fields = new ArrayList<>();
        List<Model.Method> constructors = new ArrayList<>();
        List<Model.Method> methods = new ArrayList<>();
        List<Model.TypeDecl> nested = new ArrayList<>();

        for (BodyDeclaration<?> member : td.getMembers()) {
            if (member instanceof FieldDeclaration f) {
                fields.addAll(fields(f, isInterface));
            } else if (member instanceof ConstructorDeclaration c) {
                constructors.add(callable(c, name, "constructor", c.getBody(), null, isInterface, lines, staticCollectors));
            } else if (member instanceof CompactConstructorDeclaration c) {
                constructors.add(compactConstructor(c, components, lines, staticCollectors));
            } else if (member instanceof MethodDeclaration m) {
                methods.add(callable(m, m.getNameAsString(), "method", m.getBody().orElse(null),
                        m.getType().asString(), isInterface, lines, staticCollectors));
            } else if (member instanceof TypeDeclaration<?> inner) {
                nested.add(type(inner, pkg, qualifiedName, lines, staticCollectors));
            }
        }

        return new Model.TypeDecl(
                kind,
                name,
                qualifiedName,
                visibility(td.getModifiers()),
                modifiers(td.getModifiers()),
                superclass,
                interfaces,
                annotations(td),
                typeParams,
                enumConstants,
                components,
                fields,
                constructors,
                methods,
                nested,
                startLine(td),
                endLine(td),
                ownComment(td));
    }

    // ---- members -----------------------------------------------------------------------------

    private List<Model.Field> fields(FieldDeclaration f, boolean inInterface) {
        List<Model.Field> out = new ArrayList<>();
        List<String> mods = modifiers(f.getModifiers());
        if (inInterface) {
            addMissing(mods, "public", "static", "final");
        }
        for (VariableDeclarator v : f.getVariables()) {
            out.add(new Model.Field(
                    v.getNameAsString(),
                    v.getType().asString(),
                    inInterface ? "public" : visibility(f.getModifiers()),
                    mods,
                    annotations(f),
                    startLine(f),
                    endLine(f),
                    v.getInitializer().map(i -> clip(oneLine(i.toString()), 200)).orElse(null)));
        }
        return out;
    }

    private <T extends CallableDeclaration<?>> Model.Method callable(
            T decl,
            String name,
            String kind,
            BlockStmt body,
            String returnType,
            boolean inInterface,
            String[] lines,
            boolean staticCollectors) {
        List<Model.Parameter> params = new ArrayList<>();
        decl.getParameters().forEach(p -> params.add(parameter(p)));
        List<String> thrown = new ArrayList<>();
        decl.getThrownExceptions().forEach(t -> thrown.add(t.asString()));
        List<String> typeParams = new ArrayList<>();
        for (TypeParameter tp : decl.getTypeParameters()) {
            typeParams.add(tp.asString());
        }
        NodeList<Modifier> mods = decl.getModifiers();
        boolean isPrivate = hasModifier(mods, Modifier.Keyword.PRIVATE);
        boolean isAbstract = hasModifier(mods, Modifier.Keyword.ABSTRACT)
                || (inInterface && kind.equals("method") && body == null);
        return buildMethod(decl, name, kind, mods, params, annotations(decl), thrown, typeParams, returnType,
                body, inInterface && !isPrivate, isAbstract, lines, staticCollectors);
    }

    private Model.Method compactConstructor(
            CompactConstructorDeclaration c,
            List<Model.Parameter> components,
            String[] lines,
            boolean staticCollectors) {
        List<String> thrown = new ArrayList<>();
        c.getThrownExceptions().forEach(t -> thrown.add(t.asString()));
        return buildMethod(c, c.getNameAsString(), "compact_constructor", c.getModifiers(), components,
                annotations(c), thrown, List.of(), null, c.getBody(), false, false, lines, staticCollectors);
    }

    private Model.Method buildMethod(
            Node node,
            String name,
            String kind,
            NodeList<Modifier> mods,
            List<Model.Parameter> params,
            List<Model.Annotation> annotations,
            List<String> thrown,
            List<String> typeParams,
            String returnType,
            BlockStmt body,
            boolean implicitPublic,
            boolean isAbstract,
            String[] lines,
            boolean staticCollectors) {
        int start = startLine(node);
        int end = endLine(node);

        StringBuilder sig = new StringBuilder(name).append('(');
        for (int i = 0; i < params.size(); i++) {
            Model.Parameter p = params.get(i);
            sig.append(i > 0 ? "," : "").append(p.type()).append(p.varArgs() ? "..." : "");
        }
        String signature = sig.append(')').toString();

        MethodWalker walker = new MethodWalker(staticCollectors);
        if (body != null) {
            walker.walkBody(body);
        }

        List<Model.Comment> comments = new ArrayList<>(ownComment(node));
        node.getAllContainedComments().forEach(c -> comments.add(comment(c)));

        return new Model.Method(
                name,
                signature,
                kind,
                implicitPublic ? "public" : visibility(mods),
                modifiers(mods),
                hasModifier(mods, Modifier.Keyword.STATIC),
                hasModifier(mods, Modifier.Keyword.FINAL),
                isAbstract,
                returnType,
                params,
                annotations,
                thrown,
                typeParams,
                start,
                end,
                sourceText(lines, start, end),
                body != null,
                walker.cyclomaticComplexity(),
                walker.maxNestingDepth(),
                comments,
                walker.statements(),
                walker.expressions());
    }

    // ---- small helpers -----------------------------------------------------------------------

    private Model.Parameter parameter(com.github.javaparser.ast.body.Parameter p) {
        return new Model.Parameter(
                p.getNameAsString(), p.getType().asString(), p.isVarArgs(), p.isFinal(), annotations(p));
    }

    private static List<Model.Annotation> annotations(NodeWithAnnotations<?> node) {
        List<Model.Annotation> out = new ArrayList<>();
        for (AnnotationExpr a : node.getAnnotations()) {
            out.add(new Model.Annotation(a.getNameAsString(), clip(oneLine(a.toString()), 200)));
        }
        return out;
    }

    private static List<Model.Comment> ownComment(Node node) {
        Optional<com.github.javaparser.ast.comments.Comment> c = node.getComment();
        return c.map(x -> List.of(comment(x))).orElse(List.of());
    }

    private static Model.Comment comment(com.github.javaparser.ast.comments.Comment c) {
        String kind = c.isJavadocComment() ? "javadoc" : c.isBlockComment() ? "block" : "line";
        String text = c.getContent();
        if (!c.isLineComment()) {
            // drop the decorative leading "*" of each javadoc/block line
            text = java.util.Arrays.stream(text.split("\\R"))
                    .map(l -> l.replaceFirst("^\\s*\\*+\\s?", ""))
                    .collect(java.util.stream.Collectors.joining("\n"));
        }
        return new Model.Comment(kind, startLine(c), endLine(c), text.strip());
    }

    private static List<String> modifiers(NodeList<Modifier> mods) {
        List<String> out = new ArrayList<>();
        mods.forEach(m -> out.add(m.getKeyword().asString()));
        return out;
    }

    private static boolean hasModifier(NodeList<Modifier> mods, Modifier.Keyword k) {
        return mods.stream().anyMatch(m -> m.getKeyword() == k);
    }

    private static String visibility(NodeList<Modifier> mods) {
        if (hasModifier(mods, Modifier.Keyword.PUBLIC)) return "public";
        if (hasModifier(mods, Modifier.Keyword.PROTECTED)) return "protected";
        if (hasModifier(mods, Modifier.Keyword.PRIVATE)) return "private";
        return "package";
    }

    private static void addMissing(List<String> list, String... values) {
        for (String v : values) {
            if (!list.contains(v)) list.add(v);
        }
    }

    private static int startLine(Node n) {
        return n.getRange().map(r -> r.begin.line).orElse(-1);
    }

    private static int endLine(Node n) {
        return n.getRange().map(r -> r.end.line).orElse(-1);
    }

    private static String sourceText(String[] lines, int start, int end) {
        if (start < 1 || end < start) return "";
        int to = Math.min(end, lines.length);
        return String.join("\n", java.util.Arrays.copyOfRange(lines, start - 1, to));
    }

    static String oneLine(String s) {
        return s.replaceAll("\\s+", " ").strip();
    }

    static String clip(String s, int max) {
        if (s == null) return null;
        return s.length() <= max ? s : s.substring(0, max) + "…";
    }
}

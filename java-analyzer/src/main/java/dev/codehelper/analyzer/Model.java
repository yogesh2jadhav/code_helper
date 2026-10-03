package dev.codehelper.analyzer;

import java.util.List;

/**
 * JSON result model. Component names are the wire contract with the Python side
 * (backend/app/analyzer/ast_models.py); change them in both places.
 */
public final class Model {
    private Model() {}

    public record Annotation(String name, String text) {}

    public record Import(String name, boolean isStatic, boolean isAsterisk) {}

    public record Parameter(
            String name, String type, boolean varArgs, boolean isFinal, List<Annotation> annotations) {}

    public record Field(
            String name,
            String type,
            String visibility,
            List<String> modifiers,
            List<Annotation> annotations,
            int startLine,
            int endLine,
            String initializer) {}

    /** A control/jump statement. depth = number of enclosing control structures. */
    public record Statement(
            int id, Integer parentId, String kind, int startLine, int endLine, int depth, String text) {}

    /**
     * Cheap syntactic hint about an argument or initializer, used for overload narrowing.
     * kind: literal (type = String|int|long|float|double|char|boolean|null) | name (name) |
     * new (type) | cast (type) | this | expr (exprId = a recorded call/field-access in the same
     * method, whose resolved type is the argument's type) | other.
     */
    public record ValueHint(String kind, String type, String name, Integer exprId) {
        public ValueHint(String kind, String type, String name) {
            this(kind, type, name, null);
        }
    }

    /**
     * receiverKind: none | this | super | expr (receiverExprId points at another expression of this
     * method) | cast / literal (receiverType set) | other. scopeEndLine is set on declarations
     * (variable_declaration): the last line on which the variable is in scope.
     */
    public record Expression(
            int id,
            Integer statementId,
            String kind,
            int startLine,
            int endLine,
            String text,
            String name,
            String scope,
            String type,
            Integer argCount,
            String operator,
            List<String> tags,
            String receiverKind,
            Integer receiverExprId,
            String receiverType,
            List<ValueHint> args,
            ValueHint initializer,
            Integer scopeEndLine) {}

    public record Comment(String kind, int startLine, int endLine, String text) {}

    public record Method(
            String name,
            String signature,
            String kind,
            String visibility,
            List<String> modifiers,
            boolean isStatic,
            boolean isFinal,
            boolean isAbstract,
            String returnType,
            List<Parameter> parameters,
            List<Annotation> annotations,
            List<String> throwsTypes,
            List<String> typeParameters,
            int startLine,
            int endLine,
            String sourceText,
            boolean hasBody,
            int cyclomaticComplexity,
            int maxNestingDepth,
            List<Comment> comments,
            List<Statement> statements,
            List<Expression> expressions) {}

    public record TypeDecl(
            String kind,
            String name,
            String qualifiedName,
            String visibility,
            List<String> modifiers,
            String superclass,
            List<String> interfaces,
            List<Annotation> annotations,
            List<String> typeParameters,
            List<String> enumConstants,
            List<Parameter> recordComponents,
            List<Field> fields,
            List<Method> constructors,
            List<Method> methods,
            List<TypeDecl> nestedTypes,
            int startLine,
            int endLine,
            List<Comment> comments) {}

    /** status: "ok" | "parse_error" | "analyzer_error". */
    public record FileResult(
            String path,
            String status,
            List<String> errors,
            String packageName,
            List<Import> imports,
            List<TypeDecl> types) {}
}

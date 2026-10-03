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

    /** A source range; lines and columns are 1-based, the end is inclusive. */
    public record Range(int startLine, int startColumn, int endLine, int endColumn) {}

    /**
     * A control/jump statement. depth = number of enclosing control structures. headerRange covers
     * the expression(s) that belong to the statement itself rather than its body: the condition of
     * if/else_if/while/do, the iterable of foreach, init..update of for, the selector of switch, the
     * parameter of catch, and the returned/thrown expression of return/throw.
     */
    public record Statement(
            int id,
            Integer parentId,
            String kind,
            int startLine,
            int endLine,
            int depth,
            String text,
            int startColumn,
            int endColumn,
            Range headerRange) {}

    /**
     * Cheap syntactic hint about an argument or initializer, used for overload narrowing.
     * kind: literal (type = String|int|long|float|double|char|boolean|null) | name (name) |
     * new (type) | cast (type) | this | expr (exprId = a recorded call/field-access in the same
     * method, whose resolved type is the argument's type) | other.
     */
    public record ValueHint(String kind, String type, String name, Integer exprId, Range range) {
        public ValueHint(String kind, String type, String name) {
            this(kind, type, name, null, null);
        }

        public ValueHint(String kind, String type, String name, Integer exprId) {
            this(kind, type, name, exprId, null);
        }

        public ValueHint withRange(Range r) {
            return new ValueHint(kind, type, name, exprId, r);
        }

        public ValueHint withExprId(Integer id) {
            return new ValueHint(kind, type, name, id, range);
        }
    }

    /**
     * receiverKind: none | this | super | expr (receiverExprId points at another expression of this
     * method) | cast / literal (receiverType set) | other. scopeEndLine is set on declarations
     * (variable_declaration): the last line on which the variable is in scope. initializer is the
     * value hint of a declaration's initializer or an assignment's right-hand side. left/right are
     * the operand texts of binary expressions and assignments; argTexts the call arguments' text.
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
            Integer scopeEndLine,
            int startColumn,
            int endColumn,
            String left,
            String right,
            List<String> argTexts) {}

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

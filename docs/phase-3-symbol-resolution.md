# Phase 3: symbol resolution

Resolves every reference in the parsed code to a symbol in the project, or says why it cannot.
Runs in Python over the stored ASTs (`analyzer/symbol_table.py`, `analyzer/symbol_resolver.py`);
nothing is sent to the LLM.

## Contract: three statuses, never a silent guess

| status | meaning |
|---|---|
| `resolved` | exactly one target was determined |
| `ambiguous` | several targets remain; they are listed in `candidates`, none is picked |
| `unresolved` | no target could be determined; `reason` says why |

Every non-`resolved` result has a `reason` (enforced by a test over the whole fixture project), and
every `ambiguous` result lists `candidates`.

`origin` says where a resolved target lives:

* `project`: source is in the repository, so `symbol_id`, `file` and `line` are known.
* `external`: the *type* is known (e.g. `java.util.List`) but its members cannot be verified
  because there is no source. A call such as `list.add(x)` resolves to `owner_type=java.util.List`
  with no `symbol_id`.
* `local` (variables/parameters), `primitive`, `type_variable`.

`site_line` is where the reference itself is; `file`/`line` locate the *target's* declaration.

## What is resolved

| expression kind | resolution `kind` |
|---|---|
| declared types (fields, parameters, returns, throws, `extends`, `implements`, locals) | type references with generic arguments resolved recursively |
| identifier use (`name_ref`) | `local_variable`, `parameter`, `lambda_parameter`, `catch_parameter`, `pattern_variable`, `field`, `enum_constant`, `record_component`, `static_import`, `type`, `package` |
| `a.b`, `Type.CONSTANT`, `pkg.Type` | field / enum constant / nested `type` / external member / `package` |
| method call | `method` (project or external owner) |
| `new T(...)` | `constructor` |
| `T::m`, `x::m`, `T::new` | `method_ref` |
| variable declarations | `<kind>_declaration`, carrying the declared or inferred type |

IDs are `fqn#signature` for methods (`com.acme.Foo#bar(String,int)`), `fqn#name` for fields, and
`<method id>$name@line` for locals and parameters.

## Rules followed

*Type names* resolve in Java's order: type variables, the enclosing classes and their member types
(including inherited ones), single-type imports, the same package, on-demand imports, then
`java.lang`. `extends`/`implements` are resolved in the scope *around* the class (never its own
members), which also keeps supertype lookup from recursing into itself.

*Identifiers* resolve to the innermost visible declaration: locals and parameters (matched by the
line range in which they are in scope, so sibling blocks may reuse a name with different types),
then fields of the class and its project supertypes, then enclosing classes, then static imports,
then type names, then package prefixes.

*Calls* are resolved through the receiver's static type, so chains work through project methods
(`c.getStatus().isOpen()`): unqualified calls search the innermost class that has a method of that
name (own and inherited); `this`/`super`; static calls on a type; inherited and `default` methods
(a subclass override hides the supertype method); compiler-provided members (record accessors and
canonical constructors, enum `values()`/`valueOf()`, default constructors); `Object` methods.

*Overloads* are narrowed by arity (including varargs) and by argument types where they can be
established: literals, `new T`, casts, variables, and other calls/field accesses (resolved
recursively). Primitive widening, boxing, project subtyping and `null` are modeled. The narrowing is
deliberately permissive: if an argument's type is unknown the candidate stays applicable, and if
several remain the result is `ambiguous` rather than a pick.

## Reason catalogue

`unresolved`: `unknown_type`, `external_unverifiable` (an on-demand import of a package we have no
class list for; `candidates` lists the possibilities), `unparseable_type`, `unknown_qualified_name`,
`unknown_nested_type`, `no_such_method`, `no_matching_arity`, `no_matching_constructor`,
`no_such_field`, `possible_inherited_external_method`, `possible_inherited_external_field` (a
supertype outside the project might define it), `possible_lombok_generated`,
`possible_local_class_member`, `receiver_type_unknown[:cause]`, `receiver_type_unresolved[:cause]`,
`receiver_class_unavailable`, `type_variable_receiver`, `union_receiver`, `wildcard_receiver`,
`static_wildcard_external`, `inferred_type`, `unknown_name`, `possible_package_prefix`.

`ambiguous`: `overload_not_disambiguated`, `overloaded_method_reference`,
`multiple_on_demand_imports`, `duplicate_class_definition` (the same FQN in two files, e.g. `main/`
and `test/`), `receiver_type_ambiguous`, `multiple_static_imports`, `possible_local_class_member`.

## Known limits (each surfaces as a reason, not a wrong answer)

* **No JDK member signatures.** External calls resolve to their owner type only, and the chain stops
  there: `list.stream().map(...)` has `receiver_type_unknown` because the return type of
  `List.stream()` is unknown. A small table covers `System.out/err/in`.
* **No generic substitution.** `List<Money>.get(0)` has no known type.
* **Type-variable bounds are not tracked**: `a.compareTo(b)` on `T extends Comparable<T>` is
  `type_variable_receiver`.
* **Lambda parameter types are not inferred**, so calls on them are unresolved. Their declarations
  report `inferred_type`.
* **Anonymous/local class members are not extracted.** Unqualified calls and fields inside such a
  body are reported as `possible_local_class_member`. A member of the anonymous class that shadows a
  *captured local variable* is not detected.
* **Lombok** (`@Data`, `@Getter`, ...) members are not simulated; missing members on Lombok classes
  are `possible_lombok_generated`.
* **Field initializers, static/instance initializer blocks and enum-constant bodies are not
  analyzed**, so references inside them do not appear.
* Packages outside the JDK registry (`jdk_types.py`) reached through a wildcard import stay
  `external_unverifiable`. Explicit imports and fully qualified names resolve to external types.
* Dynamic dispatch is not modeled: a call resolves to the *static* receiver type's method.

## Using it

```bash
make index SRC=/path/to/repo
PYTHONPATH=backend .venv/bin/python -m app.cli resolve /path/to/repo --examples 3
```

The report has a summary (counts per reference kind and status, calls split into project / external /
ambiguous / unresolved, reasons ranked by frequency) and up to N concrete examples per reason with
file and line. Start there to see what is worth improving for a given codebase.

## Measured

On 1,704 files of cross-referencing code (12 files replicated across 142 package namespaces):
resolution of all 30,246 references took 0.7 s; loading plus resolving 0.9 s end to end. Counts are
exact multiples of the single-copy results, which also shows no resolution leaks across namespaces.

## Changes to the analyzer output this phase needed

The AST (`AST_SCHEMA_VERSION = 2`) gained: `name_ref` expressions, receiver links
(`receiver_kind`, `receiver_expr_id`), argument hints (`args`), initializer hints, variable scope
ranges (`scope_end_line`), declarations for lambda/catch/pattern/inner-method parameters, and an
`in_local_class` tag. Analyses stored by an older analyzer are detected by the schema version and
re-analyzed automatically on the next `index`.

# SemQL Philosophy

Inspired by the Zen of Python — independent in spirit.

---

## The compiler

Infer what can be inferred. Surface what was inferred.
No decision made silently.

Compile errors are better than runtime errors.
Runtime errors are better than wrong results.
Wrong results are the only unacceptable outcome.

The compiler has no I/O.
Catalogs are Python data; the compiler returns SQL + bound params;
running the SQL is the caller's job.

## Authorisation

Identity is the caller's. Authorisation is the compiler's.
The caller knows who is asking; the compiler enforces what they may see.

`AuthContext` is request-scoped, never global.
A `Catalog` without a viewer compiles in the unscoped mode.
A `Catalog` with a viewer refuses queries touching unauthorised cubes
and injects row-level predicates inside the alias subquery.

Bypass-proof beats convenient. Scope predicates live where outer `OR`s
cannot reach them. Identity values bind as parameters, never as literals.

`expose_in_prompt` is a hint to language models — what to surface.
`required_roles` and `ScopeFn` are access control — what to allow.
Two flags, two purposes.

## SQL

The emitted SQL must be readable by the engineer debugging a production incident.
If the output is unreadable, the abstraction has failed.

Correct SQL, not optimal SQL.
The query planner is the database's job.

Raw SQL is a pragmatic escape hatch, not a feature.
When raw SQL is used, SemQL says so.
Silence implies safety.

## Errors

`compile()` fails at the first problem. `validate()` collects them all.
Two tools, two contracts.

Errors serve machines and humans.
Structure carries the meaning; `str()` carries the message.

When a field is unknown, name the closest match.
Short retry loops serve everyone — human or LLM.

## Reflection

The model is queryable through itself.
The META cubes (`catalog_cubes`, `catalog_measures`, `catalog_dimensions`)
expose the catalogue via the same compile path a normal query takes —
one compiler, one prompt contract, one execution shape.

Reflection is a design choice, not an afterthought. The catalogue
is data; querying data is what the compiler does.

## The catalog

Python is the native language for cube definitions.
Type safety, refactoring, and testing come free.

Catalogs must be serialisable and versioned.
A catalog that cannot cross a process boundary cannot serve a real system.

## Growth

One package per concern. Core stays minimal.
Dependencies you don't need should cost nothing to avoid.

Break freely before v1. Lock strictly after.
Pre-v1 is a design space. v1 is a contract.

Federation belongs above SemQL, declared explicitly.
SemQL is not Trino.

## What SemQL is not

Not an ORM. ORMs hide SQL; SemQL generates it.
The SQL is the product.

Not a BI tool. SemQL is a compiler and protocol layer.

Not a framework. It composes with your stack — it does not own it.

Human-first, LLM-friendly.
The protocol does not know who is calling.
The ergonomics are designed for people.

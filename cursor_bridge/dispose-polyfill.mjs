/**
 * Symbol.dispose / Symbol.asyncDispose polyfill — MUST load before "@cursor/sdk".
 *
 * Node 18 lacks these well-known symbols (they arrived in Node 20.4 / V8 11.4).
 * The Cursor SDK's local runtime compiles `using` declarations (explicit resource
 * management) down to tslib's __addDisposableResource, which throws
 * "TypeError: Symbol.dispose is not defined" the instant it runs on Node 18. The
 * Cursor run then ends with status="error" and EMPTY text, which the app surfaces
 * as the opaque "The model returned an empty response (no text and no tool call)".
 *
 * Defining the symbols BEFORE the SDK module is evaluated makes the SDK's
 * `[Symbol.dispose]()` methods and the tslib helper resolve to the SAME symbol, so
 * disposal works normally. Importing this as the FIRST import in every module that
 * pulls in "@cursor/sdk" (lib.mjs, list_models.mjs) guarantees that ordering: ESM
 * evaluates imported modules depth-first in source order, and this module has no
 * imports of its own.
 *
 * No-op on Node 20.4+, where both symbols are already defined (??= keeps them).
 */
Symbol.dispose ??= Symbol("Symbol.dispose");
Symbol.asyncDispose ??= Symbol("Symbol.asyncDispose");

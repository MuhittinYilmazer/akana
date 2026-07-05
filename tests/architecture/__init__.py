"""Architecture tests — static checks that lock in structural rules.

The tests in this package validate the *shape* of the codebase (module
boundaries, layering, file size), not RUNTIME behavior. The goal: protect the
structure a refactor earned (e.g. splitting a god-file into packages) from
silently regressing later.
"""

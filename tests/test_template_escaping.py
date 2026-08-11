"""Output-escaping invariant for the registration queue.

The admin Registrations page renders parent-supplied text by building HTML
strings and assigning them with innerHTML. Everything on a registration arrives
through the public, unauthenticated form, so every one of those interpolations
has to go through esc().

smoke_audit.py submits a registration full of hostile strings and checks that
approving it doesn't 500 - but nothing asserted the OUTPUT was escaped.
Stripping every esc() out of the template left both suites green (found by
review, 2026-08-10). This harness closes that hole.

The rule is inverted on purpose: an interpolation must be escaped UNLESS it is
on the reviewed allowlist below. A whitelist of "data-looking field names" was
tried first and proved worthless - it missed `${esc(value)}` -> `${value}`,
because the variable there is named `value`. Requiring an explicit exemption
means a new interpolation fails this test until somebody either escapes it or
justifies it here, which is the review gate we actually want.

Scoped deliberately to registration/admin.html. A sweep of all ~46 templates
turned up ~20 other bare interpolations and every one was a false positive: they
feed confirm() or toast(), both of which write text, not markup.

Run:  python3 tests/test_template_escaping.py
Exit 0 = all green, 1 = failures.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "templates" / "registration" / "admin.html"

# Expressions that may appear unescaped, each reviewed. Anything not listed here
# and not wrapped in esc() fails the test.
ALLOWED_UNESCAPED = {
    # Server-controlled enums and integers - never parent text.
    "r.status",
    "r.id",
    "x.id",
    "id",
    "badge(r.status)",            # maps a status enum to CSS classes
    "LABELS[x.status]",           # template-local literal map
    "new Date(r.created_at).toLocaleDateString()",
    # Pagination arithmetic.
    "p.page", "p.pages", "p.total", "p.page+1", "p.page-1",
    "p.page<=1?'disabled':''", "p.page>=p.pages?'disabled':''",
    "start", "end",
    # Presentation literals defined in this template, not data.
    "btn", "icon", "label",
    # Composition points: these return HTML whose data is already escaped by the
    # function that built it. If you add one, escape inside the function.
    "regDetails(r)",
}

# `${ ... }` including one level of nested braces (object literals, ternaries).
INTERPOLATION = re.compile(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")

results = []


def record(name, passed, detail=""):
    results.append((name, passed))
    print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail and not passed else ""))


def normalize(expr):
    return re.sub(r"\s+", " ", expr).strip()


def unescaped_interpolations(src):
    """Every interpolation that is neither escaped nor explicitly allowlisted."""
    out = []
    for raw in INTERPOLATION.findall(src):
        expr = normalize(raw)
        if not expr:
            continue
        if "esc(" in expr or ".map(esc)" in expr:
            continue
        if expr in ALLOWED_UNESCAPED:
            continue
        # A long ternary that embeds an allowlisted expression and nothing else
        # unescaped (e.g. the Approve/Reject block, which only interpolates
        # r.id) is fine; check the pieces it actually interpolates.
        inner = [normalize(i) for i in INTERPOLATION.findall(raw)]
        if inner and all(i in ALLOWED_UNESCAPED or "esc(" in i for i in inner):
            continue
        out.append(expr)
    return out


src = TEMPLATE.read_text()
record("registration queue template is readable", bool(src), "template is empty")

found = unescaped_interpolations(src)
record("every value rendered into the registration queue is escaped or reviewed",
       not found,
       "unescaped and not allowlisted: " + " || ".join(f[:80] for f in found))

# The check is only worth anything if it can fail. Strip escaping from a copy and
# confirm it fires, so a refactor can't quietly neuter this file.
stripped = src.replace("esc(", "(").replace(".map(esc)", ".map(x=>x)")
record("the check detects missing escaping (self-test)",
       len(unescaped_interpolations(stripped)) > 0,
       "the checker passed a template with all escaping removed")

record("esc() is still defined in the template",
       re.search(r"function esc\s*\(", src) is not None, "esc() helper is gone")

passed = sum(1 for _, p in results if p)
total = len(results)
print("\n" + "=" * 56)
print(f"SUMMARY: {passed}/{total} passed, {total - passed} failed.")
sys.exit(0 if passed == total else 1)

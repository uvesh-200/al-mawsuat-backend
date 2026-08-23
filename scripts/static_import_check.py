"""Static undefined-name check for all app modules (except-handler aware)."""
import ast
import builtins
from pathlib import Path

ok = True
for path in sorted(Path("app").rglob("*.py")):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            pass
        elif isinstance(node, ast.Lambda):
            defined.update(a.arg for a in node.args.args)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    missing = sorted(used - defined)
    if missing:
        ok = False
        print(f"{path}: MISSING {missing}")
print("STATIC-OK" if ok else "GAPS FOUND")

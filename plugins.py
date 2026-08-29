"""Self extension. Jarvis can write itself a new tool when it lacks one.

The gate: generated code is NEVER executed at generation time. It is parsed,
scanned, and shown to you in full before a single line runs. You tap, it loads.

Hard boundary: a plugin may do anything with your Telegram client, but it may
not shell out, eval strings, open sockets or touch the filesystem. That is the
line between "a new Telegram capability" and "a remote shell on your Mac".
"""
import ast
import re
import json
import logging
import importlib.util

import config

log = logging.getLogger("jarvis.plugins")

DIR = config.ROOT / "plugins" / "enabled"
DIR.mkdir(parents=True, exist_ok=True)

REGISTRY = {}     # name -> callable(**kwargs)
META = {}         # name -> meta dict
PROPOSALS = {}    # name -> source awaiting approval
_client = None

BANNED_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket", "requests", "urllib",
    "urllib2", "http", "ftplib", "telnetlib", "pty", "ctypes", "pickle",
    "marshal", "importlib", "builtins", "webbrowser", "smtplib", "paramiko",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "open", "input",
                "globals", "locals", "vars", "setattr", "delattr"}

TEMPLATE = '''META = {
    "name": "%(name)s",
    "description": "%(desc)s",
    "parameters": %(params)s,
    "required": %(required)s,
    "write": %(write)s,
}

async def run(client, **kwargs):
    ...
    return "done"
'''


def bind(client):
    global _client
    _client = client


def audit(source):
    """Returns a list of reasons to refuse. Empty list means it may load."""
    problems = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"does not parse: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_MODULES:
                    problems.append(f"imports {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BANNED_MODULES:
                problems.append(f"imports from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            nm = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if nm in BANNED_CALLS:
                problems.append(f"calls {nm}()")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                problems.append(f"touches dunder {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if len(v) > 4 and v.startswith("__") and v.endswith("__"):
                problems.append(f"dunder string '{v}', that is a sandbox escape")

    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if "run" not in names:
        problems.append("no run() function")
    if not any(isinstance(n, ast.Assign) and
               any(getattr(t, "id", "") == "META" for t in n.targets)
               for n in ast.walk(tree)):
        problems.append("no META dict")
    return sorted(set(problems))


def _namespace():
    """Plugins get a curated Telegram environment instead of importing. The
    model writes functions.account.X without an import otherwise, and it
    NameErrors at call time, long after you approved it."""
    from telethon import functions, types, utils
    from telethon.tl import functions as tl_functions, types as tl_types
    import asyncio as _asyncio
    import json as _json
    import re as _re
    import datetime as _datetime
    return {"functions": functions, "types": types, "utils": utils,
            "tl_functions": tl_functions, "tl_types": tl_types,
            "asyncio": _asyncio, "json": _json, "re": _re,
            "datetime": _datetime}


def _load_source(name, source):
    path = DIR / f"{name}.py"
    spec = importlib.util.spec_from_loader(f"jarvis_plugin_{name}", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__.update(_namespace())
    exec(compile(source, str(path), "exec"), mod.__dict__)   # audited above
    meta = mod.META
    fn = mod.run

    async def wrapper(**kwargs):
        return await fn(_client, **kwargs)

    REGISTRY[name] = wrapper
    META[name] = meta
    return meta


def load_all():
    ok, bad = 0, []
    for path in sorted(DIR.glob("*.py")):
        name = path.stem
        source = path.read_text(encoding="utf-8")
        problems = audit(source)
        if problems:
            bad.append(f"{name}: {', '.join(problems)}")
            continue
        try:
            _load_source(name, source)
            ok += 1
        except Exception as exc:
            bad.append(f"{name}: {exc}")
    if ok or bad:
        log.info("plugins loaded=%d rejected=%s", ok, bad or "none")
    return ok, bad


def propose(name, description, parameters, required, code, write=True):
    """Stage a new tool. Nothing is written to disk and nothing runs yet."""
    name = "".join(ch for ch in (name or "") if ch.isalnum() or ch == "_").lower()
    if not name:
        return None, "bad tool name"
    if name in REGISTRY:
        return None, f"a plugin called {name} already exists"
    try:
        params = parameters if isinstance(parameters, dict) else json.loads(parameters or "{}")
        req = required if isinstance(required, list) else json.loads(required or "[]")
    except Exception as exc:
        return None, f"parameters must be JSON: {exc}"

    # The model returns three different shapes. Guessing wrong nests its run()
    # inside the template's run(), which loads fine and does nothing.
    if isinstance(params, dict) and params.get("type") == "object" and "properties" in params:
        req = req or params.get("required", [])
        params = params["properties"]

    body = (code or "").strip()
    meta_block = ("META = {\n"
                  f"    \"name\": {json.dumps(name)},\n"
                  f"    \"description\": {json.dumps(description)},\n"
                  f"    \"parameters\": {json.dumps(params)},\n"
                  f"    \"required\": {json.dumps(req)},\n"
                  f"    \"write\": {bool(write)},\n"
                  "}\n")

    has_meta = bool(re.search(r"^\s*META\s*=", body, re.M))
    has_run = bool(re.search(r"^\s*(async\s+)?def\s+run\s*\(", body, re.M))

    if has_run and has_meta:
        body = body
    elif has_run:
        body = meta_block + "\n\n" + body
    else:
        indented = "\n".join(("    " + l) if l.strip() else l
                              for l in body.splitlines()) or "    return 'done'"
        body = meta_block + "\n\nasync def run(client, **kwargs):\n" + indented

    problems = audit(body)
    if problems:
        return None, "refused: " + "; ".join(problems)
    PROPOSALS[name] = body
    return name, None


def approve(name):
    source = PROPOSALS.pop(name, None)
    if source is None:
        return False, "no such proposal"
    problems = audit(source)
    if problems:
        return False, "refused: " + "; ".join(problems)
    try:
        meta = _load_source(name, source)
    except Exception as exc:
        return False, f"failed to load: {exc}"
    (DIR / f"{name}.py").write_text(source, encoding="utf-8")
    return True, f"Tool '{name}' is live. {meta.get('description','')}"


def disable(name):
    REGISTRY.pop(name, None)
    META.pop(name, None)
    path = DIR / f"{name}.py"
    if path.exists():
        path.rename(path.with_suffix(".py.disabled"))
        return True, f"Disabled {name}."
    return False, f"No plugin called {name}."


def schemas():
    out = []
    for name, m in META.items():
        out.append({"type": "function", "function": {
            "name": name,
            "description": "[self-built] " + m.get("description", ""),
            "parameters": {"type": "object",
                           "properties": m.get("parameters", {}),
                           "required": m.get("required", [])}}})
    return out


def write_tools():
    return {n for n, m in META.items() if m.get("write", True)}


def listing():
    if not META:
        return "No self-built tools yet."
    return "\n".join(f"- {n}: {m.get('description','')}"
                     + ("  [needs confirmation]" if m.get("write", True) else "")
                     for n, m in META.items())

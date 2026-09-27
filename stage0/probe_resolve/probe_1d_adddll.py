# -*- coding: utf-8 -*-
"""
Step 1d: HYPOTHESIS TEST -- Python 3.8+ on Windows uses safe DLL loading.
When we load fusionscript.dll by absolute path, its sibling dependencies
(lua5.1.dll, tbbmalloc.dll, ...) may NOT be resolvable, because Python
does not add the DLL's own directory to the search path.

FIX under test: os.add_dll_directory(r"C:\davinci") BEFORE loading.

Also test: switching cwd to C:\davinci (older, less reliable approach).
"""
import os
import sys
import importlib.machinery
import importlib.util

DAVINCI = r"C:\davinci"
FUSION_DLL = os.path.join(DAVINCI, "fusionscript.dll")

mode = sys.argv[1] if len(sys.argv) > 1 else "adddir"

print("=" * 70)
print(f"[1d] safe-DLL-search hypothesis test   mode={mode}")
print(f"     python {sys.version.split()[0]}  {64 if sys.maxsize > 2**32 else 32}-bit")
print("=" * 70, flush=True)

if mode == "adddir":
    print("[A] os.add_dll_directory(C:\\davinci)", flush=True)
    try:
        h = os.add_dll_directory(DAVINCI)
        print("    handle:", h, flush=True)
    except Exception as e:
        print("    FAILED:", type(e).__name__, e, flush=True)
elif mode == "cwd":
    print("[B] os.chdir(C:\\davinci) + add_dll_directory", flush=True)
    os.chdir(DAVINCI)
    try:
        os.add_dll_directory(DAVINCI)
    except Exception as e:
        print("    add_dll_directory failed:", e, flush=True)
    print("    cwd now:", os.getcwd(), flush=True)
elif mode == "none":
    print("[C] NO mitigation (control group -- expect segfault)", flush=True)

print("[*] about to load fusionscript.dll ...", flush=True)

try:
    loader = importlib.machinery.ExtensionFileLoader("fusionscript", FUSION_DLL)
    spec = importlib.util.spec_from_loader("fusionscript", loader)
    module = importlib.util.module_from_spec(spec)   # <-- previous crash point
    print("[*] SURVIVED module_from_spec  <== the fix worked up to here", flush=True)
    loader.exec_module(module)
    print("[*] SURVIVED exec_module  <== DLL loaded successfully", flush=True)
    print("[*] module:", module, flush=True)
    attrs = [a for a in dir(module) if not a.startswith("_")]
    print("[*] attrs:", attrs[:25], flush=True)
    if hasattr(module, "scriptapp"):
        print("[*] module has scriptapp() -> calling scriptapp('Resolve')", flush=True)
        app = module.scriptapp("Resolve")
        print("[*] scriptapp('Resolve') ->", app, flush=True)
        if app:
            print("[*] product:", app.GetProductName(), flush=True)
            print("[*] version:", app.GetVersionString(), flush=True)
except BaseException as e:
    print(f"[!] exception (not crash): {type(e).__name__}: {e}", flush=True)

print("=" * 70)
print("[DONE] reached end without segfault.", flush=True)

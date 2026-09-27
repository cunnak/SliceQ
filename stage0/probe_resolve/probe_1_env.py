# -*- coding: utf-8 -*-
"""
Step 1 of Stage 0.5-B: DaVinci Resolve scripting environment probe (STATIC, no GUI launch).

Goal: decouple ENVIRONMENT problems from CAPABILITY problems.
  - If import fails        -> environment/config issue (version-independent)
  - If import works but Resolve() returns None -> capability/permission issue

Evidence collected:
  1. File existence of fusionscript.dll and DaVinciResolveScript.py
  2. Env vars RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB / PYTHONPATH
  3. Whether `import DaVinciResolveScript` succeeds under three configs:
        A) with NO env vars set        (baseline / expected failure)
        B) with env vars injected      (our fix)
  4. Resolve() object availability (requires Resolve running)
"""
import os
import sys
import importlib

DAVINCI_DIR = r"C:\davinci"
SCRIPT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
FUSION_DLL = os.path.join(DAVINCI_DIR, "fusionscript.dll")
MODULE_PY = os.path.join(SCRIPT_API, "Modules", "DaVinciResolveScript.py")

SEP = "=" * 70


def section(title):
    print()
    print(SEP)
    print(title)
    print(SEP)


def main():
    print("Python:", sys.version.replace("\n", " "))
    print("Bitness:", 64 if sys.maxsize > 2**32 else 32)
    print("Executable:", sys.executable)

    # ---------- 1. files ----------
    section("[1] Required files")
    for label, path in [
        ("fusionscript.dll", FUSION_DLL),
        ("DaVinciResolveScript.py", MODULE_PY),
    ]:
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        print(f"  {'OK ' if exists else 'MISS'} {label:<28} {size:>10,} B  {path}")

    # ---------- 2. env vars ----------
    section("[2] Environment variables (current process)")
    for var in ("RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB", "PYTHONPATH"):
        val = os.getenv(var)
        print(f"  {var:<20} = {val!r}")

    # ---------- 3A. baseline import ----------
    section("[3A] Baseline: import WITHOUT env vars (expect failure)")
    baseline_ok = False
    try:
        import DaVinciResolveScript as dvr  # noqa
        baseline_ok = True
        print("  RESULT: import SUCCEEDED unexpectedly")
        print("  module:", dvr)
    except Exception as e:
        print(f"  RESULT: import FAILED -> {type(e).__name__}: {e}")
        print("  -> Confirms: env vars are REQUIRED on this machine.")

    # ---------- 3B. import with env vars injected ----------
    section("[3B] With env vars injected (our fix)")
    os.environ["RESOLVE_SCRIPT_API"] = SCRIPT_API
    os.environ["RESOLVE_SCRIPT_LIB"] = FUSION_DLL
    os.environ["PYTHONPATH"] = SCRIPT_API + r"\Modules" + os.pathsep + os.getenv("PYTHONPATH", "")
    if SCRIPT_API + r"\Modules" not in sys.path:
        sys.path.insert(0, os.path.join(SCRIPT_API, "Modules"))

    # drop any cached failed import
    for mod in ("DaVinciResolveScript", "fusionscript"):
        sys.modules.pop(mod, None)

    injected_ok = False
    dvr = None
    try:
        import DaVinciResolveScript as dvr  # noqa
        injected_ok = True
        print("  RESULT: import SUCCEEDED")
        print("  resolved module:", dvr)
        print("  module file:", getattr(dvr, "__file__", "<built-in / dynamic>"))
    except Exception as e:
        print(f"  RESULT: import FAILED -> {type(e).__name__}: {e}")

    # ---------- 4. Resolve() object ----------
    section("[4] Resolve() object (requires Resolve.exe RUNNING)")
    if injected_ok and dvr is not None:
        try:
            resolve = dvr.scriptapp("Resolve")
            if resolve is None:
                print("  RESULT: scriptapp('Resolve') returned None")
                print("  -> Environment is FINE. Resolve is simply not running,")
                print("     or the preference 'Local' scripting permission is off.")
            else:
                print("  RESULT: got Resolve handle ->", resolve)
                print("  Product:", resolve.GetProductName())
                print("  Version:", resolve.GetVersionString())
                pm = resolve.GetProjectManager()
                print("  ProjectManager:", pm)
                print("  Current project:", pm.GetCurrentProject() if pm else None)
        except Exception as e:
            print(f"  RESULT: scriptapp call raised -> {type(e).__name__}: {e}")
    else:
        print("  SKIPPED: import did not succeed, cannot proceed to step 4.")

    # ---------- summary ----------
    section("SUMMARY")
    print(f"  baseline import ok : {baseline_ok}")
    print(f"  injected import ok : {injected_ok}")
    if injected_ok and not baseline_ok:
        print("  => VERDICT: environment-only issue. Setting the two env vars fixes import.")
        print("     Next: launch Resolve, enable Local scripting in Preferences, rerun probe_2.")
    elif not injected_ok:
        print("  => VERDICT: import still fails WITH env vars. Deeper cause (bitness,")
        print("     VC runtime, or DLL dependency) — needs probe_1b.")
    else:
        print("  => VERDICT: import worked even without env vars (machine already configured).")


if __name__ == "__main__":
    main()

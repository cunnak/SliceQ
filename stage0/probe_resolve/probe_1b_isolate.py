# -*- coding: utf-8 -*-
"""
Step 1b: ISOLATE the segfault. Print progress BEFORE each risky step,
flush immediately, so we can see exactly which call kills the process.

Suspicion: the crash happens inside load_dynamic() of fusionscript.dll,
i.e. at DLL load / DllMain / init time -- not at import-time Python logic.
"""
import os
import sys

SEP = "=" * 70
DAVINCI_DIR = r"C:\davinci"
SCRIPT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
FUSION_DLL = os.path.join(DAVINCI_DIR, "fusionscript.dll")


def mark(msg):
    print(msg, flush=True)


mark(SEP)
mark("[STEP 0] process alive, python = %s" % sys.version.split()[0])
mark("         bitness = %d" % (64 if sys.maxsize > 2**32 else 32))
mark(SEP)

mark("[STEP 1] setting env vars ...")
os.environ["RESOLVE_SCRIPT_API"] = SCRIPT_API
os.environ["RESOLVE_SCRIPT_LIB"] = FUSION_DLL
os.environ["PYTHONPATH"] = SCRIPT_API + r"\Modules"
sys.path.insert(0, os.path.join(SCRIPT_API, "Modules"))
mark("         RESOLVE_SCRIPT_LIB = %s" % os.environ["RESOLVE_SCRIPT_LIB"])
mark("         done.")

mark("[STEP 2] verifying DLL exists ...")
mark("         fusionscript.dll exists = %s (%s bytes)"
     % (os.path.isfile(FUSION_DLL), os.path.getsize(FUSION_DLL) if os.path.isfile(FUSION_DLL) else -1))

mark("[STEP 3] importing importlib machinery only (safe) ...")
import importlib.machinery
import importlib.util
mark("         ok, importlib loaded.")

mark(SEP)
mark("[STEP 4] *** ABOUT TO LOAD fusionscript.dll via ExtensionFileLoader ***")
mark("         if the process dies after this line, the crash is INSIDE the DLL.")
mark(SEP)

try:
    loader = importlib.machinery.ExtensionFileLoader("fusionscript", FUSION_DLL)
    mark("[STEP 5] loader created: %r" % (loader,))

    spec = importlib.util.spec_from_loader("fusionscript", loader)
    mark("[STEP 6] spec created: %r" % (spec,))

    module = importlib.util.module_from_spec(spec)
    mark("[STEP 7] module object created: %r" % (module,))

    mark("[STEP 8] *** CALLING loader.exec_module() -- NATIVE CODE RUNS NOW ***")
    loader.exec_module(module)
    mark("[STEP 9] exec_module returned WITHOUT crash. module = %r" % (module,))
    mark("[STEP 10] module attributes: %s"
         % [a for a in dir(module) if not a.startswith("_")][:20])

except BaseException as e:
    mark("[STEP ] exception (not a crash): %s: %s" % (type(e).__name__, e))

mark(SEP)
mark("[DONE] process reached the end without segfault.")

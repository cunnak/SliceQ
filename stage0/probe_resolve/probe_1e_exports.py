# -*- coding: utf-8 -*-
"""
Step 1e: CATCH THE NATIVE FAULT with garbage-collection-off, faulthandler,
and a hard crash dump via Windows Error Reporting / procdump if available.

Approach:
 1) Enable faulthandler (catches some native faults on segv, prints C traceback).
 2) Run the load in a SUBPROCESS so the parent survives and can report exit code.
 3) Inspect the DLL's EXPORT table -- what does fusionscript.dll expose?
    Especially: does it export python-specific symbols, or a generic API?
"""
import os
import subprocess
import sys
import struct

DAVINCI = r"C:\davinci"
FUSION_DLL = os.path.join(DAVINCI, "fusionscript.dll")
HERE = os.path.dirname(os.path.abspath(__file__))


def read_exports(path):
    with open(path, "rb") as f:
        data = f.read()
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    coff = e_lfanew + 4
    machine, nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, coff)
    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    dd_off = opt + (112 if magic == 0x20B else 96)
    exp_rva, exp_size = struct.unpack_from("<II", data, dd_off)  # entry 0 = exports

    sec_off = opt + opt_size
    sections = []
    for i in range(nsec):
        s = sec_off + i * 40
        name = data[s:s + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((name, vaddr, vsize, rawptr, rawsize))

    def rva2off(rva):
        for name, vaddr, vsize, rawptr, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    if exp_rva == 0:
        return None

    o = rva2off(exp_rva)
    if o is None:
        return None
    n_funcs, n_names = struct.unpack_from("<II", data, o + 20)[0], struct.unpack_from("<I", data, o + 20)[0]
    n_funcs = struct.unpack_from("<I", data, o + 20)[0]
    n_names = struct.unpack_from("<I", data, o + 24)[0]
    addr_names = struct.unpack_from("<I", data, o + 32)[0]
    no = rva2off(addr_names)
    names = []
    for i in range(min(n_names, 500)):
        nr = struct.unpack_from("<I", data, no + i * 4)[0]
        so = rva2off(nr)
        if so is None:
            continue
        end = data.index(b"\0", so)
        names.append(data[so:end].decode("ascii", "replace"))
    return names


print("=" * 70)
print("[1e] Export table of fusionscript.dll")
print("=" * 70, flush=True)
names = read_exports(FUSION_DLL)
if names:
    print(f"exports {len(names)} symbols. Sample:")
    for n in names[:60]:
        print("   ", n)
    py = [n for n in names if "py" in n.lower()]
    print("-" * 70)
    print("python-ish exports:", py[:20] if py else "(none)")
else:
    print("could not parse exports")
print("=" * 70, flush=True)

# ---- subprocess run with faulthandler enabled ----
child = r'''
import faulthandler, os, sys
faulthandler.enable()
print("child: faulthandler enabled", flush=True)
import importlib.machinery, importlib.util
os.chdir(r"C:\davinci")
try:
    os.add_dll_directory(r"C:\davinci")
except Exception:
    pass
p = r"C:\davinci\fusionscript.dll"
print("child: loading", p, flush=True)
ldr = importlib.machinery.ExtensionFileLoader("fusionscript", p)
sp = importlib.util.spec_from_loader("fusionscript", ldr)
m = importlib.util.module_from_spec(sp)
print("child: module_from_spec OK", flush=True)
ldr.exec_module(m)
print("child: exec_module OK", flush=True)
'''
child_path = os.path.join(HERE, "_child_load.py")
with open(child_path, "w", encoding="utf-8") as f:
    f.write(child)

print("[1e-2] running child with faulthandler ...", flush=True)
r = subprocess.run([sys.executable, "-u", child_path],
                   capture_output=True, text=True, timeout=120)
print("  returncode:", r.returncode)
print("  --- stdout ---")
print(r.stdout or "(empty)")
print("  --- stderr ---")
print(r.stderr or "(empty)")
if r.returncode not in (0, 1):
    print(f"  >>> NATIVE CRASH confirmed in child (rc={r.returncode})")
    print("  >>> no Python traceback available => fault is inside native DllMain")
print("=" * 70)

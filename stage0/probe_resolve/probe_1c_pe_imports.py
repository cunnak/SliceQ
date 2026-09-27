# -*- coding: utf-8 -*-
"""
Step 1c: Read PE import table of fusionscript.dll WITHOUT loading it.
This tells us (a) what it depends on, (b) whether a pythonXX.dll is among them.
Pure byte parsing -- no LoadLibrary, so it cannot crash us.
"""
import struct
import os

DLL = r"C:\davinci\fusionscript.dll"


def read_pe_imports(path):
    with open(path, "rb") as f:
        data = f.read()

    if data[:2] != b"MZ":
        return None, None, "not a PE file"

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return None, None, "bad PE signature"

    coff = e_lfanew + 4
    machine, nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, coff)
    machine_str = {0x8664: "x64", 0x14C: "x86", 0xAA64: "ARM64"}.get(machine, hex(machine))

    opt = coff + 20
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:      # PE32+
        dd_off = opt + 112
    elif magic == 0x10B:    # PE32
        dd_off = opt + 96
    else:
        return machine_str, None, "unknown optional header magic"

    import_rva, import_size = struct.unpack_from("<II", data, dd_off + 8)  # entry 1 = imports

    # section table
    sec_off = opt + opt_size
    sections = []
    for i in range(nsec):
        s = sec_off + i * 40
        name = data[s:s + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((name, vaddr, vsize, rawptr, rawsize))

    def rva_to_off(rva):
        for name, vaddr, vsize, rawptr, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    if import_rva == 0:
        return machine_str, [], "no import table"

    off = rva_to_off(import_rva)
    if off is None:
        return machine_str, None, "cannot map import RVA"

    dlls = []
    i = 0
    while True:
        desc = off + i * 20
        oft, tstamp, fchain, name_rva, first_thunk = struct.unpack_from("<IIIII", data, desc)
        if name_rva == 0 and first_thunk == 0 and oft == 0:
            break
        no = rva_to_off(name_rva)
        if no is None:
            break
        end = data.index(b"\0", no)
        dlls.append(data[no:end].decode("ascii", "replace"))
        i += 1
        if i > 200:
            break

    return machine_str, dlls, None


print("=" * 70)
print("[fusionscript.dll] PE import analysis")
print("=" * 70)
print("path :", DLL)
print("size :", f"{os.path.getsize(DLL):,} bytes")

arch, dlls, err = read_pe_imports(DLL)
print("arch :", arch)
if err:
    print("error:", err)

if dlls is not None:
    print("-" * 70)
    print(f"imports {len(dlls)} DLLs:")
    py_deps = [d for d in dlls if "python" in d.lower()]
    for d in sorted(dlls):
        flag = "  <== PYTHON DEPENDENCY" if "python" in d.lower() else ""
        print(f"   {d}{flag}")
    print("-" * 70)
    if py_deps:
        print("VERDICT: DLL is ABI-BOUND to:", py_deps)
        print("         -> must use a matching Python version.")
    else:
        print("VERDICT: NO python*.dll dependency in the import table.")
        print("         -> It is a GENERIC bridge (embeds its own runtime or")
        print("            resolves Python symbols at runtime).")
        print("         -> Version mismatch is therefore UNLIKELY to be the cause;")
        print("            look instead at missing sibling DLLs / VC runtime.")
print("=" * 70)

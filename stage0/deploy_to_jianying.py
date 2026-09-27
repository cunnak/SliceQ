# -*- coding: utf-8 -*-
"""阶段 0 验证脚本 B：把生成草稿部署到剪映真实草稿目录，并对比元信息结构"""
import json
import os
import shutil

GEN = r"C:\Users\<user>\WorkBuddy\2026-09-23-13-06-47\SliceQ\stage0\test_drafts\SliceQ_Stage0_Test"
JY_ROOT = os.path.join(os.environ["LOCALAPPDATA"], "JianyingPro", "User Data", "Projects", "com.lveditor.draft")

print("=" * 70)
print("A. 元信息结构对比")
print("=" * 70)
gen_meta = json.load(open(os.path.join(GEN, "draft_meta_info.json"), encoding="utf-8"))
print(f"[生成草稿] 顶层键 {len(gen_meta)} 个")
for k in ["draft_name", "draft_fold_path", "draft_id", "draft_cover", "draft_json_file",
          "draft_root_path", "tm_draft_create", "draft_type"]:
    print(f"    {k} = {str(gen_meta.get(k, '<缺失>'))[:80]}")

real_dirs = [d for d in os.listdir(JY_ROOT)
             if os.path.isdir(os.path.join(JY_ROOT, d)) and not d.startswith(".")]
print(f"\n[真实草稿目录] 含 {len(real_dirs)} 个草稿: {real_dirs}")
if real_dirs:
    rp = os.path.join(JY_ROOT, real_dirs[0])
    raw = open(os.path.join(rp, "draft_meta_info.json"), "rb").read()
    print(f"[真实草稿] draft_meta_info.json 原始前 80 字节: {raw[:80]!r}")
    try:
        real_meta = json.loads(raw.decode("utf-8"))
        print(f"[真实草稿] 顶层键 {len(real_meta)} 个 —— 明文")
        for k in ["draft_name", "draft_fold_path", "draft_id", "draft_cover", "draft_json_file",
                  "draft_root_path", "tm_draft_create", "draft_type"]:
            print(f"    {k} = {str(real_meta.get(k, '<缺失>'))[:80]}")
    except Exception as e:
        print(f"[真实草稿] draft_meta_info.json 解析失败 -> {type(e).__name__}: {e}")
        print("            => 结论：剪映 10.9 对 draft_meta_info.json 同样做了加密")

print()
print("=" * 70)
print("B. 部署测试草稿到剪映草稿目录（新增，非覆盖）")
print("=" * 70)
target = os.path.join(JY_ROOT, "SliceQ_Stage0_Test")
if os.path.exists(target):
    print(f"  目标已存在，跳过: {target}")
else:
    shutil.copytree(GEN, target)
    print(f"  已部署 -> {target}")
    # 补齐剪映可能依赖的路径字段
    mp = os.path.join(target, "draft_meta_info.json")
    m = json.load(open(mp, encoding="utf-8"))
    m["draft_name"] = "SliceQ_Stage0_Test"
    m["draft_fold_path"] = target.replace("/", "\\")
    m["draft_root_path"] = JY_ROOT.replace("/", "\\")
    m["draft_json_file"] = "draft_content.json"
    m["draft_cover"] = "draft_cover.jpg"
    m["tm_draft_create"] = 1758609000000000
    m["tm_draft_modified"] = 1758609000000000
    m["draft_removable_storage_device"] = "C:"
    m["draft_type"] = ""
    json.dump(m, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
    print(f"  已补齐路径字段: draft_name / draft_fold_path / draft_root_path / draft_json_file")

print(f"\n  部署后目录内容:")
for f in sorted(os.listdir(target)):
    fp = os.path.join(target, f)
    print(f"    {f:<32} {os.path.getsize(fp) if os.path.isfile(fp) else '<dir>':>10}")
print("\n=== 验证脚本 B 执行完毕 ===")

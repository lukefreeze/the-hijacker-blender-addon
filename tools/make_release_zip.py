"""Build the installable add-on zip.

Run from the repo root:   python tools/make_release_zip.py 1.0.0-rc3

Contents = every git-tracked file under blender_sync/ PLUS the things that are
deliberately git-ignored but must ship:
  * the Windows engines (hijacker_native/win_amd64_py*/hijacker_engine.pyd)
  * the PACK-A-PUNCH transition frames (ui/assets/packapunch/frames/*.png)
  * LICENSE, NOTICE.md and THIRD_PARTY_NOTICES.md from the repo root, placed
    inside blender_sync/ (the licences of PortAudio, pybind11 and Dear ImGui
    require their notices to travel with the compiled engine)
Third-party engines/voices (Piper, AI models) are NOT included - the racks
download those on demand.
"""
import os, subprocess, sys, zipfile, glob

version = sys.argv[1] if len(sys.argv) > 1 else "dev"
out = f"TheHijacker-{version}.zip"

files = [f for f in subprocess.check_output(
    ["git", "ls-files", "-z", "blender_sync"], text=True).split("\0")
    if f and os.path.isfile(f)]
extra = glob.glob("blender_sync/hijacker_native/win_amd64_py*/hijacker_engine.pyd")
extra += glob.glob("blender_sync/ui/assets/packapunch/frames/*.png")
frames = [e for e in extra if "/frames/" in e]
if not frames:
    sys.exit("ERROR: no PACK-A-PUNCH frames found - the intro video would not play")
for e in extra:
    if e not in files:
        files.append(e)

# (source file in repo root, name inside the add-on folder)
LICENCE_FILES = [("LICENSE", "blender_sync/LICENSE"),
                 ("NOTICE.md", "blender_sync/NOTICE.md"),
                 ("THIRD_PARTY_NOTICES.md", "blender_sync/THIRD_PARTY_NOTICES.md")]
for src, _ in LICENCE_FILES:
    if not os.path.isfile(src):
        sys.exit(f"ERROR: {src} is missing - the zip must ship its licence files")

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for src, arc in LICENCE_FILES:
        zi = zipfile.ZipInfo.from_file(src, arc)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o100644 << 16
        with open(src, "rb") as fh:
            z.writestr(zi, fh.read())
    for f in sorted(files):
        zi = zipfile.ZipInfo.from_file(f, f)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = ((0o755 if f.endswith(".so") else 0o644) | 0o100000) << 16
        with open(f, "rb") as fh:
            z.writestr(zi, fh.read())
print(f"{out}: {len(files) + len(LICENCE_FILES)} files, {os.path.getsize(out)/1e6:.1f} MB, "
      f"{len(frames)} transition frames, testzip={zipfile.ZipFile(out).testzip()}")

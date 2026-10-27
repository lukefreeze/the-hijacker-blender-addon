"""Build the installable add-on zip.

Run from the repo root:   python tools/make_release_zip.py 1.0.0-rc3

Contents = every git-tracked file under blender_sync/ PLUS the things that are
deliberately git-ignored but must ship:
  * the Windows engines (hijacker_native/win_amd64_py*/hijacker_engine.pyd)
  * the PACK-A-PUNCH transition frames (ui/assets/packapunch/frames/*.png)
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

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for f in sorted(files):
        zi = zipfile.ZipInfo.from_file(f, f)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = ((0o755 if f.endswith(".so") else 0o644) | 0o100000) << 16
        with open(f, "rb") as fh:
            z.writestr(zi, fh.read())
print(f"{out}: {len(files)} files, {os.path.getsize(out)/1e6:.1f} MB, "
      f"{len(frames)} transition frames, testzip={zipfile.ZipFile(out).testzip()}")

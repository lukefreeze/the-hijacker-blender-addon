#!/usr/bin/env python3
"""
Hijacker audio engine test runner.

Builds the real engine sources (src/hijacker_audio_engine.cpp and
src/hijacker_processor.cpp) against a fake PortAudio - a "sound card" that
needs no audio hardware - and runs the test scripts against that build.

    python tests/engine/run_tests.py                 # build + functional tests
    python tests/engine/run_tests.py --stress        # + concurrency stress (plain)
    python tests/engine/run_tests.py --sanitize      # + stress under ASan/UBSan and TSan
    python tests/engine/run_tests.py --pybind        # + test the real pybind11 module surface
    python tests/engine/run_tests.py --only 02 06    # run just some tests

Needs: a C++17 compiler (g++ or clang++, set CXX to choose), python3, numpy and scipy.
Optional: `pip install bpy` (Blender as a Python module, Python 3.11) enables the keyframed-volume test.
Works on Linux and macOS. (The Windows/MSVC build is covered by build.bat / CI.)
"""
import argparse, glob, os, shutil, subprocess, sys, sysconfig, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BUILD = os.path.join(HERE, "_build")
NATIVE = os.path.join(HERE, "native")
INCLUDES = ["-I", os.path.join(ROOT, "include"),
            "-I", os.path.join(ROOT, "include", "audaspace_stub"),
            "-I", os.path.join(HERE, "fake_portaudio"),
            "-I", NATIVE]
ENGINE_SRC = [os.path.join(ROOT, "src", "hijacker_audio_engine.cpp"),
              os.path.join(ROOT, "src", "hijacker_processor.cpp")]
CXX = os.environ.get("CXX") or shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


def run(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def build_lib():
    ext = ".dylib" if sys.platform == "darwin" else ".so"
    out = os.path.join(BUILD, "libhj_test" + ext)
    cmd = [CXX, "-O2", "-g", "-std=c++17", "-Wall", "-shared", "-fPIC", *INCLUDES,
           os.path.join(NATIVE, "capi.cpp"), os.path.join(NATIVE, "pa_fake.cpp"), *ENGINE_SRC,
           "-pthread", "-o", out]
    r = run(cmd)
    if r.returncode:
        print(r.stdout); sys.exit("engine build failed")
    warns = [l for l in r.stdout.splitlines() if "warning" in l]
    if warns:
        print(f"  ({len(warns)} compiler warnings)")
    return out


def build_stress(kind):
    flags = {"plain": ["-DCOUNT_ALLOCS"],
             "asan": ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"],
             "tsan": ["-fsanitize=thread"]}[kind]
    out = os.path.join(BUILD, "stress_" + kind)
    cmd = [CXX, "-O1", "-g", "-std=c++17", *flags, *INCLUDES, os.path.join(NATIVE, "stress.cpp"),
           os.path.join(NATIVE, "pa_fake.cpp"), *ENGINE_SRC, "-pthread", "-o", out]
    r = run(cmd)
    if r.returncode:
        print(r.stdout); return None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress", action="store_true")
    ap.add_argument("--sanitize", action="store_true")
    ap.add_argument("--pybind", action="store_true")
    ap.add_argument("--stress-seconds", type=float, default=6.0)
    ap.add_argument("--only", nargs="*", default=None, help="test numbers, e.g. 02 06")
    a = ap.parse_args()
    if not CXX:
        sys.exit("no C++ compiler found (set CXX)")
    os.makedirs(BUILD, exist_ok=True)
    results = []

    print(f"[build] {CXX}")
    lib = build_lib()

    tests = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if a.only:
        tests = [t for t in tests if any(os.path.basename(t).startswith("test_" + n) for n in a.only)]
    for t in tests:
        name = os.path.basename(t)[:-3]
        t0 = time.time()
        r = run([sys.executable, t, lib])
        ok = r.returncode == 0
        results.append((name, ok, time.time() - t0))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({time.time() - t0:.1f}s)")
        if not ok:
            print("\n".join("    " + l for l in r.stdout.splitlines()[-25:]))

    stress_kinds = (["plain"] if (a.stress or a.sanitize) else []) + (["asan", "tsan"] if a.sanitize else [])
    for kind in stress_kinds:
        exe = build_stress(kind)
        if not exe:
            results.append((f"stress_{kind} (build)", False, 0.0)); print(f"[FAIL] stress_{kind}: build"); continue
        env = dict(os.environ)
        if kind == "tsan":
            env["TSAN_OPTIONS"] = "suppressions=" + os.path.join(NATIVE, "tsan.supp") + " exitcode=66"
        if kind == "asan":
            env["ASAN_OPTIONS"] = "detect_leaks=0"
            env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
        t0 = time.time()
        r = run([exe, str(a.stress_seconds)], env=env)
        out = r.stdout
        good = r.returncode == 0 and "0 bad-output callbacks" in out
        if kind == "plain":
            good = good and "allocations in callback: 0" in out
        if kind == "tsan":
            good = good and "WARNING: ThreadSanitizer" not in out
        results.append((f"stress_{kind}", good, time.time() - t0))
        print(f"[{'PASS' if good else 'FAIL'}] stress_{kind}  ({time.time() - t0:.1f}s)  " +
              next((l for l in out.splitlines() if l.startswith("stress done")), ""))
        if not good:
            print("\n".join("    " + l for l in out.splitlines()[-30:]))

    if a.pybind:
        try:
            import pybind11  # noqa: F401
        except ImportError:
            print("[SKIP] pybind: pybind11 not installed")
        else:
            ok = run_pybind()
            results.append(("pybind_module", ok, 0.0))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


def run_pybind():
    import pybind11
    ext = sysconfig.get_config_var("EXT_SUFFIX")
    out_dir = os.path.join(BUILD, "pymod"); os.makedirs(out_dir, exist_ok=True)
    cmd = [CXX, "-O2", "-shared", "-fPIC", "-std=c++17", *INCLUDES, "-I", os.path.join(ROOT, "src"),
           "-I", pybind11.get_include(), "-I", sysconfig.get_paths()["include"],
           os.path.join(ROOT, "src", "wrapper.cpp"), *ENGINE_SRC,
           os.path.join(NATIVE, "pa_fake.cpp"), os.path.join(NATIVE, "fake_run_c.cpp"),
           os.path.join(ROOT, "src", "mixer_ui.cpp"), "-pthread",
           "-o", os.path.join(out_dir, "hijacker_engine" + ext)]
    if sys.platform == "darwin":
        cmd += ["-undefined", "dynamic_lookup"]
    r = run(cmd)
    if r.returncode:
        print(r.stdout[-3000:]); print("[FAIL] pybind module build"); return False
    ok = True
    for t in sorted(glob.glob(os.path.join(HERE, "pybind", "test_*.py"))):
        name = os.path.basename(t)[:-3]
        r = run([sys.executable, t, out_dir])
        good = r.returncode == 0
        ok &= good
        print(f"[{'PASS' if good else 'FAIL'}] pybind/{name}")
        if not good:
            print("\n".join("    " + l for l in r.stdout.splitlines()[-30:]))
    return ok


if __name__ == "__main__":
    main()

# VMC harness CLI installer (spec 10). Served by apps/api: GET /harness/install.py
#   macOS:   curl -fsSL https://api.vibemafiaclub.com/harness/install.py | python3 -
#   Windows: irm https://api.vibemafiaclub.com/harness/install.py | python -
#   offline: python install.py            (from the offline bundle)
# Installs only the CLI shell (no core logic): ~/.vmc-harness/bin/ + PATH. Then downloads the kernel (no license key needed).
#
# The shell source lives in engine/vmc_harness/dist/shell.py (single source). scripts/release.py renders this file with
# SHELL_B64 filled in and every non-ASCII character escaped, so piping through Windows PowerShell 5.1 (ASCII) is safe.
# Keep this file free of non-ASCII outside normal (non-raw) string literals.
import base64
import sys
import types

SHELL_B64 = None  # filled by scripts/release.py render_install()


def _shell_source():
    if SHELL_B64:
        return base64.b64decode(SHELL_B64).decode("utf-8")
    from pathlib import Path
    here = Path(__file__).resolve().parent  # repo checkout (not rendered)
    return (here.parent / "engine" / "vmc_harness" / "dist" / "shell.py").read_text(encoding="utf-8")


def main(argv=None):
    if sys.version_info < (3, 9):
        sys.stderr.write("VMC harness needs Python 3.9 or newer (found %d.%d)\n" % sys.version_info[:2])
        return 2
    src = _shell_source()
    mod = types.ModuleType("vmc_harness_shell")
    mod.__file__ = "vmc_harness_shell.py"
    sys.modules["vmc_harness_shell"] = mod
    exec(compile(src, "vmc_harness_shell.py", "exec"), mod.__dict__)
    mod._utf8_stdio()
    return mod.install_self(src, sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

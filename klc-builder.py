"""Build a Windows keyboard driver (DLLs + MSI) directly from a .klc file."""

import os
import platform
import re
import subprocess
import sys
import webbrowser
from pathlib import Path
from shutil import copyfile, move, rmtree

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

MSKLC_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=102134"

ARCHS = {"i386": "-x", "amd64": "-m", "ia64": "-i", "wow64": "-o"}
HOST_ARCH = {"amd64": "amd64", "x86": "i386", "arm64": "amd64"}


# --- environment ------------------------------------------------------------

def find_msklc(hint=None):
    """Return the MSKLC install dir, or None if it isn't installed."""
    dirs = [Path(hint)] if hint else []
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            dirs += sorted(Path(base).glob("Microsoft Keyboard Layout Creator*"))

    for d in dirs:
        if (d / "MSKLC.exe").exists() and (d / "bin/i386/kbdutool.exe").exists():
            return d
    return None


def check_environment(hint=None):
    """Make sure we're on Windows with MSKLC installed. Exits if not."""
    if sys.platform != "win32":
        sys.exit(f"Error: Windows is required (running on {platform.system()}).")

    msklc_dir = find_msklc(hint)
    if msklc_dir:
        return msklc_dir

    print("Microsoft Keyboard Layout Creator 1.4 is not installed.")
    print(f"Download it here: {MSKLC_URL}")
    if input("Open the download page now? [y/N] ").strip().lower() == "y":
        webbrowser.open(MSKLC_URL)
    sys.exit("Install MSKLC, then run this again.")


# --- klc --------------------------------------------------------------------

def read_klc(path: Path) -> str:
    """Read a .klc file (MSKLC saves UTF-16LE, usually with BOM)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 3 and raw[1] == 0 and raw[3] == 0:
        return raw.decode("utf-16-le")
    return raw.decode("utf-8-sig", errors="replace")


def klc_name(text: str) -> str:
    """The name on the KBD line: drives the DLL, MSI and folder names."""
    match = re.search(r"^KBD\s+(\S+)", text, re.MULTILINE)
    if not match:
        raise ValueError("no KBD line found, is this really a .klc file?")
    name = match.group(1).strip('"')
    if len(name) > 8:
        raise ValueError(f"KBD name `{name}` is longer than 8 characters")
    return name


# --- builder ----------------------------------------------------------------

class MsklcManager:
    def __init__(self, klc: Path, msklc_dir=None, out_dir=None, verbose=False):
        self.msklc_dir = Path(msklc_dir) if msklc_dir else check_environment()
        self.out_dir = Path(out_dir or Path.cwd()).resolve()
        self.verbose = verbose

        klc = Path(klc).resolve()
        self.name = klc_name(read_klc(klc))
        self.package = self.out_dir / self.name

        # kbdutool names its output after the file, so it must be <name>.klc
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.klc = self.out_dir / f"{self.name}.klc"
        if klc != self.klc:
            copyfile(klc, self.klc)

    def _run(self, *args):
        return subprocess.run([str(a) for a in args], capture_output=not self.verbose, text=True)

    def build_package(self) -> bool:
        """Let MSKLC create the installer package (setup.exe + per-arch MSIs)."""

        print("[+] Build setup.exe")
        if (self.package / "setup.exe").exists():
            return True

        result = self._run(self.msklc_dir / "MSKLC.exe", self.klc, "-build")

        # MSKLC always drops the package in "My Documents"
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
        built = Path(buf.value) / self.name

        if not (built / "setup.exe").exists():
            print(f"MSKLC failed ({result.returncode}); the layout may already be "
                  "installed, or too complex for MSKLC's builder.")
            print(result.stdout or "", result.stderr or "")
            return False

        move(str(built), str(self.package))
        return True

    def build_dlls(self) -> bool:
        """Compile the .klc into one DLL per architecture and put them in the package."""

        print("[+] Build Dlls")
        kbdutool = self.msklc_dir / "bin/i386/kbdutool.exe"
        dll = self.klc.with_suffix(".dll")
        prev = os.getcwd()
        os.chdir(self.out_dir)
        try:
            for arch, flag in ARCHS.items():
                arch_dir = self.package / arch
                rmtree(arch_dir, ignore_errors=True)
                arch_dir.mkdir(parents=True)

                print(f"[!] building {arch}...")
                result = self._run(kbdutool, "-u", flag, self.klc.name)
                if result.returncode != 0 or not dll.exists():
                    print(f"kbdutool failed for {arch}:")
                    print(result.stdout or "", result.stderr or "")
                    return False
                move(str(dll), str(arch_dir / dll.name))
        finally:
            os.chdir(prev)
        return True

    def install(self) -> bool:
        arch = HOST_ARCH.get(platform.machine().lower())
        msi = self.package / f"{self.name}_{arch}.msi"
        if not msi.exists():
            print(f"`{msi}` not found")
            return False
        return self._run("msiexec.exe", "/i", msi).returncode == 0

    def build(self) -> bool:
        return self.build_package() and self.build_dlls()


if __name__ == "__main__":
    msklc_dir = check_environment()
    
    if len(sys.argv) < 2:
        sys.exit("usage: python msklc_manager.py <layout.klc> [--install] [-v]")

    print(f"[+] MSKLC: {msklc_dir}")

    manager = MsklcManager(Path(sys.argv[1]), msklc_dir, verbose="-v" in sys.argv)

    if not manager.build():
        sys.exit(1)
    print(f"[+] Done: {manager.package}")

    if "--install" in sys.argv:
        sys.exit(0 if manager.install() else 1)
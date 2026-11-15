import argparse
import ctypes
import json
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request
import winreg
from pathlib import Path


def get_exe_dir() -> Path:
    """Directory where the .exe (or .py script) resides—used for persistent state."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


def get_manifest_path() -> Path:
    """Resolve bundled packages.json when frozen, or local packages.json in dev."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "packages.json"
        if bundled.exists():
            return bundled
    return get_exe_dir() / "packages.json"


# Configuration paths
BASE_DIR = get_exe_dir()
MANIFEST_FILE = get_manifest_path()
STATE_FILE = BASE_DIR / "installed.json"


def _normalize_name(name: str) -> str:
    """Normalize package/display names by removing spaces, hyphens, and underscores."""
    return re.sub(r"[\s\-_]+", "", name.strip().lower())


def _parse_uninstall_cmd(cmd_str: str, is_uninstall_string: bool) -> list[str]:
    """Split a registry uninstall command into [exe_path, ...args] safely."""
    parts = [p.strip('"') for p in shlex.split(cmd_str, posix=False) if p.strip('"')]
    if not parts:
        return []

    exe_path = parts[0]
    args = parts[1:]

    if is_uninstall_string and exe_path.lower().endswith(".exe"):
        lower_args = [a.lower() for a in args]
        if not any(flag in lower_args for flag in ("/s", "/verysilent", "/silent", "/quiet")):
            args.append("/S")

    return [exe_path] + args


def is_admin() -> bool:
    """Check if the script is running with Windows Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except AttributeError:
        return False


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def download_file(url: str, dest: Path) -> None:
    """Download a file from a URL with a console progress indicator."""
    print(f"Downloading from {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 PyPM/1.0"})

    with urllib.request.urlopen(req) as response, open(dest, "wb") as out_file:
        total_size = int(response.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 8192

        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            out_file.write(chunk)
            downloaded += len(chunk)

            if total_size > 0:
                percent = (downloaded / total_size) * 100
                sys.stdout.write(f"\rProgress: {percent:.1f}% ({downloaded // 1024} KB)")
                sys.stdout.flush()
    print("\nDownload complete.")


def run_installer(file_path: Path, pkg_type: str, silent_args: list[str]) -> bool:
    """Execute the .exe or .msi installer silently."""
    if pkg_type == "msi":
        cmd = ["msiexec", "/i", str(file_path), "/qn", "/norestart"] + silent_args
    elif pkg_type == "exe":
        cmd = [str(file_path)] + silent_args
    else:
        print(f"Error: Unsupported installer type '{pkg_type}'.")
        return False

    print(f"Installing... Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"Installation failed with exit code {e.returncode}.")
        return False
    except Exception as e:
        print(f"Unexpected error during installation: {e}")
        return False


def find_uninstall_command(package_name: str, pkg_type: str, product_code: str = "") -> list[str] | None:
    """Locate an uninstall command for an MSI product code or via Windows Registry."""
    if pkg_type == "msi" and product_code:
        return ["msiexec", "/x", product_code, "/qn", "/norestart"]

    hives = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    needle = _normalize_name(package_name)

    for hive, subkey_path in hives:
        try:
            with winreg.OpenKey(hive, subkey_path) as root_key:
                count = winreg.QueryInfoKey(root_key)[0]
                for i in range(count):
                    try:
                        subkey_name = winreg.EnumKey(root_key, i)
                        with winreg.OpenKey(root_key, subkey_name) as app_key:
                            display_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                            if needle and needle in _normalize_name(str(display_name)):
                                for value_name in ("QuietUninstallString", "UninstallString"):
                                    try:
                                        cmd_str, _ = winreg.QueryValueEx(app_key, value_name)
                                        if cmd_str:
                                            parsed = _parse_uninstall_cmd(
                                                str(cmd_str).strip(),
                                                is_uninstall_string=(value_name == "UninstallString"),
                                            )
                                            if parsed:
                                                return parsed
                                    except FileNotFoundError:
                                        continue
                    except OSError:
                        continue
        except OSError:
            continue

    return None


def install_package(package_name: str, direct_url: str = None, silent_args: list[str] = None) -> None:
    if not is_admin():
        print("Error: Administrator privileges are required to install software.")
        print("Please restart your terminal as Administrator.")
        sys.exit(1)

    manifest = load_json(MANIFEST_FILE)
    installed = load_json(STATE_FILE)

    if direct_url:
        pkg_type = "msi" if direct_url.lower().endswith(".msi") else "exe"
        pkg_info = {
            "version": "custom",
            "type": pkg_type,
            "url": direct_url,
            "silent_args": silent_args or [],
        }
    elif package_name in manifest:
        pkg_info = manifest[package_name]
    else:
        print(f"Error: Package '{package_name}' not found in manifest.")
        sys.exit(1)

    url = pkg_info["url"]
    pkg_type = pkg_info["type"].lower()
    args = pkg_info.get("silent_args", [])
    ext = f".{pkg_type}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        installer_path = Path(tmp_dir) / f"{package_name}_installer{ext}"
        try:
            download_file(url, installer_path)
        except Exception as e:
            print(f"Download failed: {e}")
            sys.exit(1)

        if run_installer(installer_path, pkg_type, args):
            print(f"Successfully installed '{package_name}'!")
            installed[package_name] = {
                "version": pkg_info["version"],
                "type": pkg_type,
                "url": url,
            }
            save_json(STATE_FILE, installed)
        else:
            print(f"Failed to install '{package_name}'.")
            sys.exit(1)


def remove_package(package_name: str) -> None:
    """Uninstall a package and remove it from installed.json."""
    if not is_admin():
        print("Error: Administrator privileges are required to remove software.")
        print("Please restart your terminal as Administrator.")
        sys.exit(1)

    manifest = load_json(MANIFEST_FILE)
    installed = load_json(STATE_FILE)

    if package_name not in installed and package_name not in manifest:
        print(f"Error: Package '{package_name}' is not installed or not in manifest.")
        sys.exit(1)

    inst_info = installed.get(package_name, {})
    man_info = manifest.get(package_name, {})
    pkg_type = (inst_info.get("type") or man_info.get("type") or "exe").lower()
    product_code = inst_info.get("product_code", "")

    cmd = find_uninstall_command(package_name, pkg_type=pkg_type, product_code=product_code)

    if cmd:
        print(f"Removing '{package_name}'... Running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Uninstaller returned exit code {e.returncode}.")
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error during removal: {e}")
            sys.exit(1)
    else:
        print(f"Warning: Could not find an automatic uninstaller in Registry for '{package_name}'.")
        print("Removing record from installed tracking only.")

    if package_name in installed:
        del installed[package_name]
        save_json(STATE_FILE, installed)
        print(f"Removed '{package_name}' from installed packages.")


def search_packages(query: str) -> None:
    """Search packages in the manifest by case-insensitive name match."""
    manifest = load_json(MANIFEST_FILE)
    installed = load_json(STATE_FILE)
    q = query.strip().lower()

    matches = [
        (name, info)
        for name, info in manifest.items()
        if q in name.lower()
    ]

    if not matches:
        print(f"No packages found matching '{query}'.")
        return

    print(f"Found {len(matches)} package(s) matching '{query}':\n")
    print(f"{'PACKAGE':<24} {'AVAILABLE':<12} {'INSTALLED':<12} {'TYPE':<6} {'URL'}")
    print("-" * 92)
    for name, info in matches:
        inst_ver = installed.get(name, {}).get("version", "-")
        print(
            f"{name:<24} "
            f"{info.get('version', '-'):<12} "
            f"{inst_ver:<12} "
            f"{info.get('type', '-'):<6} "
            f"{info.get('url', '')}"
        )


def list_packages() -> None:
    manifest = load_json(MANIFEST_FILE)
    installed = load_json(STATE_FILE)

    print(f"{'PACKAGE':<24} {'AVAILABLE':<12} {'INSTALLED':<12} {'TYPE'}")
    print("-" * 58)
    for name, info in manifest.items():
        inst_ver = installed.get(name, {}).get("version", "-")
        print(f"{name:<24} {info['version']:<12} {inst_ver:<12} {info['type']}")


def main():
    parser = argparse.ArgumentParser(description="wlpi - Windows Lightweight Package Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Install command
    install_parser = subparsers.add_parser("install", help="Install a package")
    install_parser.add_argument("package", help="Name of the package")
    install_parser.add_argument("--url", help="Direct URL to an .exe or .msi file", default=None)
    install_parser.add_argument("--args", nargs="*", help="Custom silent flags for direct URLs", default=[])

    # Remove command
    remove_parser = subparsers.add_parser("remove", help="Uninstall a package")
    remove_parser.add_argument("package", help="Name of the package to remove")

    # Search command
    search_parser = subparsers.add_parser("search", help="Search available packages by name")
    search_parser.add_argument("query", help="Search term (case-insensitive)")

    # List command
    subparsers.add_parser("list", help="List available and installed packages")

    args = parser.parse_args()

    if args.command == "install":
        install_package(args.package, direct_url=args.url, silent_args=args.args)
    elif args.command == "remove":
        remove_package(args.package)
    elif args.command == "search":
        search_packages(args.query)
    elif args.command == "list":
        list_packages()


if __name__ == "__main__":
    main()
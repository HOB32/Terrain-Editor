"""Find Steam installs on whatever machine the hub is running on.

The hub folder lives in OneDrive, so it follows the user between computers,
but Steam does not: one PC keeps its library on E:, the next on C:. Hard-coded
paths meant every new machine crashed on start-up. Instead, Steam is asked
where its libraries are (registry, then libraryfolders.vdf), with a sweep of
the usual folders on every drive as a fallback.
"""
import os
import re
import string
from pathlib import Path

H1Z1 = ("H1Z1",)
JUST_SURVIVE = ("Just Survive",)
KOTK_DEPOT = ("app_433850", "depot_433851")


def _registry_steam_dirs():
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive, key, value in [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")]:
        try:
            with winreg.OpenKey(hive, key) as handle:
                found.append(winreg.QueryValueEx(handle, value)[0])
        except OSError:
            pass
    return found


def _drives():
    if os.name != "nt":
        return []
    return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


def steam_libraries():
    """Every Steam library folder (the one holding `steamapps`) on this machine."""
    roots = list(_registry_steam_dirs())
    for drive in _drives():
        roots += [os.path.join(drive, "Program Files (x86)", "Steam"), os.path.join(drive, "Program Files", "Steam"),
                  os.path.join(drive, "Steam"), os.path.join(drive, "SteamLibrary"),
                  os.path.join(drive, "Games", "Steam"), os.path.join(drive, "Games", "SteamLibrary")]
    libraries, seen = [], set()
    for root in roots:
        vdf = Path(root, "steamapps", "libraryfolders.vdf")
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        extra = [m.replace("\\\\", "\\") for m in re.findall(r'"path"\s+"([^"]+)"', text)]
        for candidate in [root, *extra]:
            key = os.path.normcase(os.path.abspath(candidate))
            if key not in seen and Path(candidate, "steamapps").is_dir():
                seen.add(key)
                libraries.append(str(Path(candidate).resolve()))
    return libraries


def find_assets(game=H1Z1):
    """Resources/Assets of an installed game (common/<name>) or a downloaded depot
    (content/<app>/<depot>), or None when it is not on this machine."""
    for library in steam_libraries():
        base = Path(library, "steamapps", "content" if game == KOTK_DEPOT else "common", *game)
        assets = base / "Resources" / "Assets"
        if assets.is_dir():
            return str(assets)
    return None


if __name__ == "__main__":
    print("Steam libraries:", *steam_libraries() or ["(none found)"], sep="\n  ")
    for label, game in [("H1Z1", H1Z1), ("Just Survive", JUST_SURVIVE), ("KotK depot", KOTK_DEPOT)]:
        print(f"{label}: {find_assets(game) or 'not installed'}")

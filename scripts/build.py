"""
Build client and server artifacts for the modpack.

Produces:
  build/<name>-<version>.mrpack       client artifact (Modrinth format)
  build/<name>-<version>-server.zip   server artifact (jars + configs + start scripts)

Reads:
  pack.toml                MC + loader version, name, version
  mods/*.pw.toml           Modrinth-tracked mods (one file per mod)
  mods/*.jar               local mods (bundled directly into both artifacts)
  config/                  mod config files (bundled into both)
  resourcepacks/           optional, client-side bundle
  shaderpacks/             optional, client-side bundle

`side` field in each .pw.toml controls which artifact a mod ships in:
  side = "both"    -> client + server
  side = "client"  -> client only
  side = "server"  -> server only

Usage:
  python scripts/build.py [--out build] [--cache .build_cache]

No packwiz install required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

UA = "skyratlandia-modpack-build/0.1"


@dataclass
class Mod:
    name: str
    filename: str
    side: str
    url: str
    sha512: str
    sha1: str = ""
    size: int = 0
    local_path: Path | None = None  # only set for bundled local jars


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_pack(root: Path) -> dict:
    with open(root / "pack.toml", "rb") as f:
        return tomllib.load(f)


def load_mod_metafiles(root: Path) -> list[Mod]:
    mods_dir = root / "mods"
    out: list[Mod] = []
    for pw in sorted(mods_dir.glob("*.pw.toml")):
        with open(pw, "rb") as f:
            d = tomllib.load(f)
        download = d.get("download") or {}
        if download.get("hash-format") != "sha512":
            fail(f"{pw.name}: only sha512 download hashes supported, got {download.get('hash-format')}")
        out.append(Mod(
            name=d.get("name", pw.stem),
            filename=d.get("filename") or fail(f"{pw.name}: missing filename"),  # type: ignore[arg-type]
            side=d.get("side", "both"),
            url=download.get("url") or fail(f"{pw.name}: missing download.url"),  # type: ignore[arg-type]
            sha512=download.get("hash") or fail(f"{pw.name}: missing download.hash"),  # type: ignore[arg-type]
        ))
    return out


def load_local_jars(root: Path) -> list[Mod]:
    """Raw .jar files dropped into mods/ — bundled into both artifacts."""
    mods_dir = root / "mods"
    out: list[Mod] = []
    for jar in sorted(mods_dir.glob("*.jar")):
        data = jar.read_bytes()
        out.append(Mod(
            name=jar.stem,
            filename=jar.name,
            side="both",
            url="",  # no remote URL — bundled directly
            sha512=hashlib.sha512(data).hexdigest(),
            sha1=hashlib.sha1(data).hexdigest(),
            size=len(data),
            local_path=jar,
        ))
    return out


def download_with_cache(url: str, expected_sha512: str, cache_dir: Path) -> Path:
    """Download url to cache_dir, keyed by sha512. Returns path to cached file."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / expected_sha512
    if target.exists():
        # Verify hash on cache hit (cheap insurance against corruption).
        if hashlib.sha512(target.read_bytes()).hexdigest() == expected_sha512:
            return target
        target.unlink()
    print(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except urllib.error.URLError as e:
        fail(f"download failed for {url}: {e}")
    actual = hashlib.sha512(data).hexdigest()
    if actual != expected_sha512:
        fail(f"hash mismatch for {url}\n  expected sha512: {expected_sha512}\n  got:             {actual}")
    target.write_bytes(data)
    return target


def hydrate_remote_mods(mods: list[Mod], cache_dir: Path) -> None:
    """Download each remote mod, fill in sha1 + size."""
    for m in mods:
        if m.local_path is not None:
            continue  # already hydrated
        path = download_with_cache(m.url, m.sha512, cache_dir)
        data = path.read_bytes()
        m.sha1 = hashlib.sha1(data).hexdigest()
        m.size = len(data)
        m.local_path = path


def env_for(side: str) -> dict[str, str]:
    """Modrinth env flags from packwiz side."""
    if side == "client":
        return {"client": "required", "server": "unsupported"}
    if side == "server":
        return {"client": "unsupported", "server": "required"}
    return {"client": "required", "server": "required"}


def build_mrpack(
    out_path: Path,
    pack: dict,
    mods: list[Mod],
    overrides: list[tuple[Path, str]],  # (src, arcname-under-overrides)
    summary: str = "",
) -> None:
    """Build a Modrinth .mrpack at out_path."""
    files = []
    for m in mods:
        if m.local_path is None:
            continue  # local jar — goes in overrides instead
        if not m.url:
            continue
        files.append({
            "path": f"mods/{m.filename}",
            "hashes": {"sha1": m.sha1, "sha512": m.sha512},
            "env": env_for(m.side),
            "downloads": [m.url],
            "fileSize": m.size,
        })

    deps: dict[str, str] = {"minecraft": pack["versions"]["minecraft"]}
    for k in ("forge", "neoforge", "fabric-loader", "quilt-loader"):
        if k in pack["versions"]:
            deps[k] = pack["versions"][k]

    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": str(pack.get("version", "0.0.0")),
        "name": pack.get("name", "modpack"),
        "summary": summary,
        "files": files,
        "dependencies": deps,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modrinth.index.json", json.dumps(index, indent=2))
        # Local jars become client-side overrides — bundled directly so they
        # land in .minecraft/mods/ on import.
        for m in mods:
            if m.local_path is not None and not m.url:
                z.write(m.local_path, f"overrides/mods/{m.filename}")
        for src, arcname in overrides:
            z.write(src, f"overrides/{arcname}")


def build_server_zip(
    out_path: Path,
    pack: dict,
    mods: list[Mod],
    overrides: list[tuple[Path, str]],
) -> None:
    """Build a server zip: mods/, config/, start scripts, README."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mc = pack["versions"]["minecraft"]
    nf = pack["versions"].get("neoforge", "")
    name = pack.get("name", "modpack")

    start_sh = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "cd \"$(dirname \"$0\")\"\n"
        f"# NeoForge server jar name pattern: neoforge-{nf}-server.jar (or run/run.sh after install)\n"
        "if [ -f run.sh ]; then\n"
        "  exec ./run.sh \"$@\"\n"
        "fi\n"
        "echo 'NeoForge server not installed. Run the NeoForge installer first:'\n"
        f"echo '  java -jar neoforge-{nf}-installer.jar --installServer'\n"
        "echo 'Then re-run this script.'\n"
        "exit 1\n"
    )
    start_bat = (
        "@echo off\r\n"
        "cd /d %~dp0\r\n"
        "if exist run.bat (\r\n"
        "  call run.bat %*\r\n"
        "  exit /b\r\n"
        ")\r\n"
        "echo NeoForge server not installed. Run the installer first:\r\n"
        f"echo   java -jar neoforge-{nf}-installer.jar --installServer\r\n"
        "echo Then re-run this script.\r\n"
        "pause\r\n"
        "exit /b 1\r\n"
    )
    readme = (
        f"# {name} - Server\n\n"
        f"Minecraft: {mc}\n"
        f"NeoForge:  {nf}\n\n"
        "## Setup\n\n"
        "1. Install Java 21 (NeoForge 1.21.x requires JDK 21).\n"
        f"2. Download the NeoForge installer from https://projects.neoforged.net/neoforged/neoforge\n"
        f"   (pick version {nf}) and run:\n"
        f"     java -jar neoforge-{nf}-installer.jar --installServer\n"
        "3. Accept the EULA: open `eula.txt` and set `eula=true`.\n"
        "4. Start the server: `./start.sh` (Linux/Mac) or `start.bat` (Windows).\n\n"
        "## Files in this zip\n\n"
        "- `mods/`     server-side mods (already downloaded)\n"
        "- `config/`   mod configs\n"
        "- `start.sh`, `start.bat`  launcher wrappers around the NeoForge `run` script\n\n"
        "Mods marked client-only in the pack are excluded from this zip on purpose.\n"
    )

    server_mods = [m for m in mods if m.side in ("both", "server")]
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for m in server_mods:
            assert m.local_path is not None
            z.write(m.local_path, f"mods/{m.filename}")
        for src, arcname in overrides:
            # Only ship config/ — resourcepacks/shaderpacks are client-only.
            if arcname.startswith("config/"):
                z.write(src, arcname)
        z.writestr("start.sh", start_sh)
        z.writestr("start.bat", start_bat)
        z.writestr("README.md", readme)


def collect_overrides(root: Path) -> list[tuple[Path, str]]:
    """Tracked override files (configs + optional client packs)."""
    out: list[tuple[Path, str]] = []
    for sub in ("config", "resourcepacks", "shaderpacks"):
        d = root / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.name != ".gitkeep":
                out.append((f, f.relative_to(root).as_posix()))
    return out


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in s).strip("-").lower() or "modpack"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--out", type=Path, default=None, help="output dir (default: <root>/build)")
    ap.add_argument("--cache", type=Path, default=None, help="jar download cache (default: <root>/.build_cache)")
    ap.add_argument("--clean", action="store_true", help="wipe output dir first")
    args = ap.parse_args()

    root = args.root.resolve()
    out_dir = (args.out or root / "build").resolve()
    cache_dir = (args.cache or root / ".build_cache").resolve()

    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pack = load_pack(root)
    name_slug = slugify(str(pack.get("name", "modpack")))
    version = str(pack.get("version", "0.0.0"))

    print(f"Pack: {pack.get('name')} v{version}")
    print(f"  MC {pack['versions']['minecraft']}  loaders={list(pack['versions'].keys())[1:]}")

    remote_mods = load_mod_metafiles(root)
    local_mods = load_local_jars(root)
    print(f"Mods: {len(remote_mods)} remote + {len(local_mods)} local")
    sides = {"both": 0, "client": 0, "server": 0}
    for m in remote_mods + local_mods:
        sides[m.side] = sides.get(m.side, 0) + 1
    print(f"  sides: both={sides['both']}  client-only={sides['client']}  server-only={sides['server']}")

    print("\nDownloading remote mods to cache ...")
    hydrate_remote_mods(remote_mods, cache_dir)

    overrides = collect_overrides(root)
    print(f"Overrides: {len(overrides)} files (config/resourcepacks/shaderpacks)")

    all_mods = remote_mods + local_mods

    mrpack_path = out_dir / f"{name_slug}-{version}.mrpack"
    server_path = out_dir / f"{name_slug}-{version}-server.zip"

    print(f"\nBuilding {mrpack_path.name} ...")
    build_mrpack(mrpack_path, pack, all_mods, overrides)
    print(f"  {mrpack_path.stat().st_size:,} bytes")

    print(f"\nBuilding {server_path.name} ...")
    build_server_zip(server_path, pack, all_mods, overrides)
    print(f"  {server_path.stat().st_size:,} bytes")

    print("\nDone.")


if __name__ == "__main__":
    main()

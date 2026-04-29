"""
Build modpack artifacts for both Modrinth and CurseForge launchers.

Produces:
  build/<name>-<version>.mrpack       Modrinth-format pack (small, URL refs)
  build/<name>-<version>-curseforge.zip  CurseForge-format pack (jars bundled)

Reads:
  pack.toml                MC + loader version, name, version
  mods/*.pw.toml           Modrinth-tracked mods (one file per mod)
  mods/*.jar               local mods (bundled into overrides/mods/)
  config/                  mod config files (bundled into overrides/)
  resourcepacks/           optional, bundled into overrides/
  shaderpacks/             optional, bundled into overrides/

CurseForge zip uses the all-overrides format (no CF API key needed): every
mod jar is bundled directly. Larger artifact but works without per-mod
CurseForge project IDs and avoids needing a CF API key in CI.

`side` is forwarded to the mrpack's env flags. CurseForge's manifest spec
has no per-mod side concept, so the CF zip ships every mod and lets the
client load what it can.

Usage:
  python scripts/build.py [--out build] [--cache .build_cache]
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


def build_curseforge_zip(
    out_path: Path,
    pack: dict,
    mods: list[Mod],
    overrides: list[tuple[Path, str]],
) -> None:
    """Build a CurseForge-format modpack zip with every jar bundled in
    overrides/mods/. Empty `files` array — no CF API key needed."""
    versions = pack["versions"]
    mc_version = versions["minecraft"]
    if "neoforge" in versions:
        loader_id = f"neoforge-{versions['neoforge']}"
    elif "forge" in versions:
        loader_id = f"forge-{versions['forge']}"
    elif "fabric-loader" in versions:
        loader_id = f"fabric-{versions['fabric-loader']}"
    elif "quilt-loader" in versions:
        loader_id = f"quilt-{versions['quilt-loader']}"
    else:
        fail("pack.toml has no recognized mod loader (neoforge/forge/fabric/quilt)")

    manifest = {
        "minecraft": {
            "version": mc_version,
            "modLoaders": [{"id": loader_id, "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": pack.get("name", "modpack"),
        "version": str(pack.get("version", "0.0.0")),
        "author": pack.get("author", ""),
        "files": [],
        "overrides": "overrides",
    }

    modlist_rows = "\n".join(
        f"  <li><a href=\"{m.url}\">{m.filename}</a></li>" if m.url
        else f"  <li>{m.filename} (bundled local)</li>"
        for m in mods
    )
    modlist_html = f"<ul>\n{modlist_rows}\n</ul>\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("modlist.html", modlist_html)
        # Every mod jar — both downloaded-from-Modrinth and dropped-in-locally —
        # goes into overrides/mods/ so the CF launcher just extracts them.
        for m in mods:
            assert m.local_path is not None, f"{m.filename}: jar not hydrated"
            z.write(m.local_path, f"overrides/mods/{m.filename}")
        for src, arcname in overrides:
            z.write(src, f"overrides/{arcname}")


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
    print(f"\nBuilding {mrpack_path.name} ...")
    build_mrpack(mrpack_path, pack, all_mods, overrides)
    print(f"  {mrpack_path.stat().st_size:,} bytes")

    cf_path = out_dir / f"{name_slug}-{version}-curseforge.zip"
    print(f"\nBuilding {cf_path.name} ...")
    build_curseforge_zip(cf_path, pack, all_mods, overrides)
    print(f"  {cf_path.stat().st_size:,} bytes")

    print("\nDone.")


if __name__ == "__main__":
    main()

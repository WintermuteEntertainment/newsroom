#!/usr/bin/env python3
"""Builds the Chrome and Firefox packages from one shared source tree.

The two browsers differ only in the manifest: Firefox requires an explicit extension id
under browser_specific_settings (AMO will not assign one for MV3), and Chrome rejects that
key as unrecognised. Everything else -- markup, styling, and all the logic -- is byte
identical, which is the point: a forked codebase would drift.

    python build.py            # writes dist/chrome/ and dist/firefox/
    python build.py --zip      # also writes the two loadable/uploadable archives

Cross-browser API differences are handled at runtime by compat.js, not here.
"""
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path


def copy_contents(src: Path, dst: Path) -> None:
    """Copies bytes, and permissions only when the filesystem allows it.

    shutil.copy/copy2 also replicate mode and mtime, which raises EPERM on some mounted
    filesystems (WSL mounts of NTFS, network shares, container bind mounts). That metadata is
    irrelevant to a browser loading an extension, so failing to copy it is not a failure to
    build -- but the bytes themselves are never allowed to fail quietly.
    """
    dst.write_bytes(src.read_bytes())
    try:
        shutil.copymode(src, dst)
    except OSError:
        pass


ROOT = Path(__file__).parent
DIST = ROOT / "dist"

# Shared files, copied verbatim into both builds.
SHARED = [
    "compat.js", "digest.js", "digest.css",
    "newtab.html", "newtab.js",
    "popup.html", "popup.js",
    "options.html", "options.js",
]

TARGETS = {"chrome": "manifest.chrome.json", "firefox": "manifest.firefox.json"}


def check_integrity() -> list[str]:
    """Detects a source tree assembled from files of different vintages.

    Every refusal in check_sources() below is a real defect, but when someone has downloaded
    files individually the SAME symptom appears six times over and reads as six separate bugs
    rather than one cause: a mixed-vintage tree. SOURCES.sha256 (shipped in the source zip)
    lets us say that plainly instead of listing symptoms.

    Absent that file this returns nothing, so a working tree without it still builds.
    """
    stamp = ROOT / "SOURCES.sha256"
    if not stamp.exists():
        return []

    import hashlib
    expected = {}
    for line in stamp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        expected[name.strip()] = digest.strip()

    mismatched, missing = [], []
    for name, want in sorted(expected.items()):
        path = ROOT / name
        if not path.exists():
            missing.append(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            mismatched.append(name)

    if not (mismatched or missing):
        return []

    lines = ["this source tree does not match the released set, so the files come from "
             "different versions and will not work together:"]
    for name in missing:
        lines.append(f"    {name} -- missing")
    for name in mismatched:
        lines.append(f"    {name} -- differs from the release")
    lines.append("  fix: download newsroom-digest-source.zip and extract it to an EMPTY folder,")
    lines.append("  rather than collecting files one at a time. If you edited a file on purpose,")
    lines.append("  delete SOURCES.sha256 to skip this check.")
    return ["\n".join(lines)]


def check_sources() -> list[str]:
    """Catches the mistakes that only show up as a silently broken extension."""
    problems = check_integrity()
    if problems:
        return problems

    for name in SHARED + list(TARGETS.values()):
        if not (ROOT / name).exists():
            problems.append(f"missing source file: {name}")
    if problems:
        return problems

    # compat.js must load before any file that uses `storage` or `openOptions`.
    for page in ("newtab.html", "popup.html", "options.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        scripts = [line for line in html.splitlines() if "<script" in line]
        joined = "\n".join(scripts)
        if "compat.js" not in joined:
            problems.append(f"{page} does not load compat.js")
        elif "digest.js" in joined and joined.index("compat.js") > joined.index("digest.js"):
            problems.append(f"{page} loads compat.js after digest.js")
        elif "options.js" in joined and joined.index("compat.js") > joined.index("options.js"):
            problems.append(f"{page} loads compat.js after options.js")

    # No direct chrome.* outside the shim -- that is what breaks Firefox's promise API.
    for name in ("digest.js", "options.js", "newtab.js", "popup.js"):
        body = (ROOT / name).read_text(encoding="utf-8")
        if "chrome." in body:
            problems.append(f"{name} calls chrome.* directly; route it through compat.js")

    # Firefox will not sign an MV3 extension without an explicit id.
    ff = json.loads((ROOT / TARGETS["firefox"]).read_text(encoding="utf-8"))
    if not ff.get("browser_specific_settings", {}).get("gecko", {}).get("id"):
        problems.append("manifest.firefox.json has no browser_specific_settings.gecko.id")

    # Chrome rejects the whole manifest if that key is present.
    chrome_mf = json.loads((ROOT / TARGETS["chrome"]).read_text(encoding="utf-8"))
    if "browser_specific_settings" in chrome_mf:
        problems.append("manifest.chrome.json carries browser_specific_settings; Chrome rejects it")

    # A version skew between the two builds makes bug reports ambiguous.
    if chrome_mf.get("version") != ff.get("version"):
        problems.append(f"version skew: chrome {chrome_mf.get('version')} vs firefox {ff.get('version')}")

    # Every file each surface references must exist in the build.
    for page in ("newtab.html", "popup.html", "options.html"):
        html = (ROOT / page).read_text(encoding="utf-8")
        for token in ("compat.js", "digest.js", "options.js", "newtab.js", "popup.js", "digest.css"):
            if token in html and token not in SHARED:
                problems.append(f"{page} references {token}, which the build does not copy")

    return problems


def build(target: str, manifest_name: str) -> Path:
    out = DIST / target
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for name in SHARED:
        copy_contents(ROOT / name, out / name)
    copy_contents(ROOT / manifest_name, out / "manifest.json")
    return out


def make_zip(folder: Path) -> Path:
    archive = DIST / f"newsroom-digest-{folder.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder))
    return archive


SOURCE_FILES = SHARED + list(TARGETS.values()) + [
    "build.py", "README.md", "INSTALL.md", "run_tests.sh", "test_compat.mjs", "test_refresh_client.mjs",
    # test_outlet_chips.mjs runs against a REAL captured /api/digest response rather than a
    # hand-written fixture, so the fixture ships with it or the suite cannot run.
    "test_outlet_chips.mjs", "live_fixture.json",
]


def write_source_zip() -> Path:
    """Packages the source tree as ONE file, with a checksum manifest inside it.

    This exists because handing someone a list of files to download individually does not work:
    they end up with files from different versions, and the symptom is a page of build refusals
    that all look like separate bugs. One archive cannot be assembled wrongly.
    """
    import hashlib

    stamp_lines = ["# sha256 of each released source file -- checked by build.py.",
                   "# Delete this file if you are editing the sources on purpose."]
    for name in sorted(SOURCE_FILES):
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        stamp_lines.append(f"{digest}  {name}")
    stamp = ROOT / "SOURCES.sha256"
    stamp.write_text("\n".join(stamp_lines) + "\n", encoding="utf-8")

    out = ROOT / "newsroom-digest-source.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(SOURCE_FILES) + ["SOURCES.sha256"]:
            archive.write(ROOT / name, f"extension/{name}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", action="store_true", help="also write the packaged archives")
    parser.add_argument("--source-zip", action="store_true",
                       help="write newsroom-digest-source.zip (the whole tree, with checksums)")
    args = parser.parse_args()

    if args.source_zip:
        out = write_source_zip()
        print(f"  source   {out.name}  ({len(SOURCE_FILES) + 1} files, checksummed)")
        return 0

    problems = check_sources()
    if problems:
        print("build refused:")
        for p in problems:
            print(f"  - {p}")
        return 1

    for target, manifest_name in TARGETS.items():
        folder = build(target, manifest_name)
        line = f"  {target:<8} {folder.relative_to(ROOT)}  ({len(list(folder.iterdir()))} files)"
        if args.zip:
            line += f"  -> {make_zip(folder).name}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

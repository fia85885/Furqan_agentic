#!/usr/bin/env python3
"""
check_requirements.py -- will this requirements.txt install on Python 3.11?

Checks every dependency line against the PyPI JSON API and reports:
  1. Dependencies checked
  2. Not found on PyPI          (package missing, or pinned version missing)
  3. Incompatible with Python 3.11 (requires_python excludes 3.11)
  4. Highest requirement        (release with the highest minimum Python)

The verdict is always for Python 3.11, whatever interpreter runs this script.

Usage:  python check_requirements.py [requirements.txt]
Needs:  pip install packaging
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

TARGET = "3.11"
# A requires_python like ">=3.11.4" excludes 3.11.0 but not 3.11.9, so
# "compatible with 3.11" means: at least one 3.11.x release is allowed.
TARGET_VERSIONS = ["3.11.%d" % z for z in range(0, 30)]
# Used to evaluate environment markers ("; python_version < '3.11'") as 3.11
MARKER_ENV = {"python_version": TARGET, "python_full_version": TARGET + ".0"}

PYPI = "https://pypi.org/pypi"
USER_AGENT = "check-requirements/1.0 (python-3.11 compatibility check)"


class NotFound(Exception):
    """PyPI returned 404."""


# --------------------------------------------------------------------------- #
# PyPI access
# --------------------------------------------------------------------------- #
def fetch_json(url, retries=3):
    """GET a PyPI JSON URL. Raises NotFound on 404, RuntimeError on other failures."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            if e.code < 500 and e.code != 429:
                raise RuntimeError("HTTP %d for %s" % (e.code, url))
            err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            err = e
            if 'getaddrinfo failed' in str(e) or '[Errno 11001]' in str(e):
                break
        time.sleep(1.5 * (attempt + 1))
    # A network error must never be reported as "not found on PyPI"
    raise RuntimeError("could not reach PyPI (%s): %s" % (url, err))


# --------------------------------------------------------------------------- #
# requires_python helpers
# --------------------------------------------------------------------------- #
def parse_requires_python(raw):
    """Return SpecifierSet, None (no requirement), or raise InvalidSpecifier."""
    if raw is None or not raw.strip():
        return None
    # Some old releases have trailing commas / stray spaces
    cleaned = ",".join(p.strip() for p in raw.split(",") if p.strip())
    return SpecifierSet(cleaned)


def allows_target(spec):
    return any(spec.contains(v, prereleases=True) for v in TARGET_VERSIONS)


def minimum_python(spec):
    """Lowest Python version the specifier can accept (Version('0') if unbounded)."""
    lowest = Version("0")
    for s in spec:
        if s.operator in (">=", ">", "~=", "==", "==="):
            v = s.version
            if v.endswith(".*"):
                v = v[:-2]
            try:
                lowest = max(lowest, Version(v))
            except InvalidVersion:
                pass
    return lowest


# --------------------------------------------------------------------------- #
# requirements.txt parsing
# --------------------------------------------------------------------------- #
def parse_requirement_lines(raw_lines):
    """Yield (line_no, text) for dependency lines (comments/blank lines/options skipped)."""
    buf, start = "", None
    for no, line in enumerate(raw_lines, 1):
        if start is None:
            start = no
        if line.rstrip().endswith("\\"):          # line continuation
            buf += line.rstrip()[:-1] + " "
            continue
        buf += line
        text = buf.split(" #")[0].split("\t#")[0].strip()   # inline comment
        buf, first = "", start
        start = None
        if not text or text.startswith("#"):
            continue
        if text.startswith("-"):                  # -r, -e, --index-url, ...
            print("warning: line %d skipped (pip option, not checked): %s"
                  % (first, text), file=sys.stderr)
            continue
        yield first, text


def read_requirement_lines(path):
    """Read a requirements file and yield (line_no, text)."""
    with open(path, encoding="utf-8-sig") as f:
        raw_lines = f.read().splitlines()
    yield from parse_requirement_lines(raw_lines)


# --------------------------------------------------------------------------- #
# Checking one line
# --------------------------------------------------------------------------- #
def check_line(line_no, text):
    """Return a dict describing the result for one dependency line."""
    res = {"line": line_no, "text": text, "status": None, "detail": "",
           "label": text, "requires_python": None, "min_python": None}

    try:
        req = Requirement(text)
    except InvalidRequirement as e:
        res.update(status="invalid", detail="unparseable line: %s" % e)
        return res

    if req.marker is not None and not req.marker.evaluate(MARKER_ENV):
        res.update(status="skipped",
                   detail="marker '%s' is false on Python %s, pip would not install it"
                          % (req.marker, TARGET))
        return res

    name = canonicalize_name(req.name)
    pins = [s for s in req.specifier if s.operator in ("==", "===")
            and not s.version.endswith(".*")]

    try:
        project = fetch_json("%s/%s/json" % (PYPI, name))
    except NotFound:
        res.update(status="not_found", detail="package does not exist on PyPI")
        return res
    except RuntimeError as e:
        res.update(status="error", detail=str(e))
        return res

    if not pins:
        res.update(status="unpinned",
                   detail="no exact '==' pin; package exists, version not checked")
        return res

    pinned = pins[0].version
    res["label"] = "%s==%s" % (req.name, pinned)

    # Find the release the pin refers to. PEP 440 equality, not string equality:
    # '==1.26' matches release '1.26.0', but /pypi/numpy/1.26/json would 404.
    release_key = None
    releases = project.get("releases") or {}
    if pinned in releases:
        release_key = pinned
    else:
        try:
            want = Version(pinned)
            for key in releases:
                try:
                    if Version(key) == want:
                        release_key = key
                        break
                except InvalidVersion:
                    continue
        except InvalidVersion:
            pass
    if release_key is None:
        release_key = pinned          # let the version endpoint decide

    try:
        rel = fetch_json("%s/%s/%s/json" % (PYPI, name, release_key))
    except NotFound:
        res.update(status="not_found",
                   detail="package exists, but version %s does not" % pinned)
        return res
    except RuntimeError as e:
        res.update(status="error", detail=str(e))
        return res

    raw_rp = (rel.get("info") or {}).get("requires_python")
    try:
        spec = parse_requires_python(raw_rp)
    except InvalidSpecifier:
        # pip ignores an unparseable Requires-Python, so it does not block install
        res.update(status="ok",
                   detail="invalid requires_python %r ignored (pip ignores it too)" % raw_rp)
        return res

    if spec is None:
        res.update(status="ok", detail="no requires_python (counts as compatible)")
        return res

    res["requires_python"] = str(raw_rp).strip()
    res["min_python"] = minimum_python(spec)

    if allows_target(spec):
        res.update(status="ok")
    else:
        res.update(status="incompatible",
                   detail="requires_python %s excludes Python %s"
                          % (res["requires_python"], TARGET))
    return res


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    lines = []

    if len(sys.argv) > 1:
        target = sys.argv[1]
        if os.path.isfile(target):
            lines = list(read_requirement_lines(target))
        elif target == "-":
            lines = list(parse_requirement_lines(sys.stdin.read().splitlines()))
        else:
            # Treat CLI arguments directly as package specifications
            raw_args = [arg.strip() for arg in sys.argv[1:] if arg.strip()]
            lines = list(parse_requirement_lines(raw_args))
    else:
        # Check current working directory, then script directory
        default_path = "requirements.txt"
        if not os.path.isfile(default_path):
            script_req = os.path.join(script_dir, "requirements.txt")
            if os.path.isfile(script_req):
                default_path = script_req

        if os.path.isfile(default_path):
            lines = list(read_requirement_lines(default_path))
        elif not sys.stdin.isatty():
            lines = list(parse_requirement_lines(sys.stdin.read().splitlines()))
        else:
            print("No 'requirements.txt' found in current directory (%s)." % os.getcwd())
            print("Usage: python main.py [requirements.txt | package_spec ...]")
            sys.exit(1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda lt: check_line(*lt), lines))

    def by(status):
        return [r for r in results if r["status"] == status]

    not_found = by("not_found")
    incompatible = by("incompatible")

    # ---- Output ----
    print("1. Dependencies checked: %d" % len(results))

    print("2. Not found on PyPI: %d" % len(not_found))
    for r in not_found:
        print("   - %s  (line %d: %s)" % (r["label"], r["line"], r["detail"]))

    print("3. Incompatible with Python %s: %d" % (TARGET, len(incompatible)))
    for r in incompatible:
        print("   - %s  (requires_python %s)" % (r["label"], r["requires_python"]))

    # Among existing releases that HAVE a requires_python (compatible or not),
    # the one with the highest minimum Python. Ties: first in the file wins.
    candidates = [r for r in results if r["requires_python"] is not None]
    if candidates:
        best = candidates[0]
        for r in candidates[1:]:
            if r["min_python"] > best["min_python"]:
                best = r
        print("4. Highest requirement: %s (%s)" % (best["label"], best["requires_python"]))
    else:
        print("4. Highest requirement: none (no release declares requires_python)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stamp index.html with a build id and write a matching version.json.

GitHub Pages serves index.html with `cache-control: max-age=600`, so an open tab
keeps running the old page for up to ten minutes after a deploy - long enough to
download a chart built by code that has already been replaced. The page checks
version.json (uncached) against the id baked into it and reloads itself once when
they differ. This script keeps the two in sync.

Run before committing:  python3 tools/stamp.py
"""
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(HERE, "index.html")
VERSION = os.path.join(HERE, "version.json")
PATTERN = re.compile(r"const BUILD = '([^']*)';")

html = open(INDEX).read()
if not PATTERN.search(html):
    raise SystemExit("no BUILD constant in index.html - nothing to stamp")

# Hash the page with the stamp neutralised, so the id tracks real content changes
# and is stable if the script runs twice on identical markup.
neutral = PATTERN.sub("const BUILD = '';", html)
build = hashlib.sha256(neutral.encode()).hexdigest()[:12]

stamped = PATTERN.sub(f"const BUILD = '{build}';", html)
if stamped != html:
    open(INDEX, "w").write(stamped)

with open(VERSION, "w") as f:
    json.dump({"build": build}, f)
    f.write("\n")

print(f"build {build}")

#!/usr/bin/env python3
"""Flatten index.html + data.json into one self-contained page.

Used to publish a point-in-time copy of the dashboard (e.g. as a Claude Artifact)
that needs no server: the host wraps the file in its own <!doctype>/<head>/<body>,
and a strict CSP blocks cross-origin fetches, so the document wrappers come off and
data.json goes inline as a literal instead of being fetched.

    python3 tools/build_snapshot.py [output.html]

Defaults to snapshot.html beside the repo (gitignored).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "snapshot.html")

html = open(os.path.join(HERE, "index.html")).read()
data = json.load(open(os.path.join(HERE, "data.json")))

style = re.search(r"<style>.*?</style>", html, re.S).group(0)
fonts = "".join(re.findall(r'<link rel="stylesheet" href="https://fonts\.googleapis\.com[^"]*">', html))
body = re.search(r"<body>(.*?)\n<script>", html, re.S).group(1)
script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)

script, n = re.subn(r"fetch\('data\.json'.*?\}\);\s*$", "render(DATA);\n", script, flags=re.S)
assert n == 1, f"expected to replace exactly one fetch block, replaced {n}"

# `</` inside a JSON string would close the script element early.
payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

# Dated off the close, not the build machine's clock, which has been observed skewed.
banner = (
    '<div class="snapshot">Static snapshot of '
    '<a href="https://phummy1337.github.io/state-of-dfdv/">phummy1337.github.io/state-of-dfdv</a>'
    f', prices through the {data["as_of"]["price_date"]} close. '
    "The live page refreshes on a schedule; this one does not.</div>"
)
body = body.replace('<div class="wrap">', f'<div class="wrap">\n  {banner}', 1)

extra_css = """
<style>
.snapshot{
  margin:18px 0 -6px; padding:9px 13px; font-size:12.5px; color:var(--ink-2);
  background:var(--panel); border:1px solid var(--line);
  border-left:2px solid var(--orange); border-radius:9px;
}
</style>
"""

out = (f"<title>State of DFDV</title>\n{fonts}\n{style}{extra_css}{body}\n"
       f"<script>\nconst DATA = {payload};\n{script}</script>\n")
with open(OUT, "w") as f:
    f.write(out)
print(f"wrote {OUT}  ({len(out)/1024:.0f} KB)")

#!/usr/bin/env python3
import sys
import re

COAUTHOR = "Co-Authored-By: Holden Karau <holden@pigscanfly.ca>"

msg = sys.stdin.read()

# Already present (case-insensitive on header, tolerant on whitespace/email)
pat = re.compile(r"(?im)^\s*co-authored-by:\s*.*<\s*holden@pigscanfly\.ca\s*>\s*$")
if COAUTHOR in msg or pat.search(msg):
    sys.stdout.write(msg)
    raise SystemExit(0)

if not msg.endswith("\n"):
    msg += "\n"
if not msg.endswith("\n\n"):
    msg += "\n"

msg += COAUTHOR + "\n"
sys.stdout.write(msg)

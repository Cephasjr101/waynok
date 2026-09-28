#!/usr/bin/env python3
"""Apply UI/UX Pro Max accessibility checklist to Waynok's index.html.

Adds:
  - prefers-reduced-motion support (skill: motion only when user allows it)
  - text selection color matching the accent
  - explicit cursor:pointer on clickable rows/chips that lacked it
Run from the repo root:  python apply_a11y_patch.py   (safe to re-run)
"""
import os

PATH = "src/static/index.html" if os.path.exists("src/static/index.html") else "static/index.html"

ADD = """
  /* --- ui-ux-pro-max: motion + interaction checklist --- */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }
  ::selection { background: rgba(255,176,32,.28); color: #fff; }
  .msg, .notif-item, .conv, .fchip, .tr { cursor: pointer; }
  /* --- /ui-ux-pro-max checklist --- */
"""

with open(PATH, encoding="utf-8") as f:
    html = f.read()

if "prefers-reduced-motion" in html:
    print("skip - a11y patch already applied")
    raise SystemExit(0)

anchor = "</style>"
if html.count(anchor) != 1:
    raise SystemExit(f"ABORT: expected one </style>, found {html.count(anchor)}")

html = html.replace(anchor, ADD + "\n</style>")
with open(PATH, "w", encoding="utf-8") as f:
    f.write(html)
print("OK - a11y checklist applied to " + PATH)

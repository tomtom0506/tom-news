#!/usr/bin/env python3
"""מחשב את ה-hash של הסקריפט ב-docs/index.html ומעדכן את ה-CSP. להריץ אחרי כל שינוי בסקריפט:
python3 tests/csp_hash.py"""
import base64, hashlib, os, re, sys

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "index.html")


def script_hash(page):
    m = re.search(r"<script>(.*?)</script>", page, re.S)
    return base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()


if __name__ == "__main__":
    page = open(PAGE, encoding="utf-8").read()
    new = re.sub(r"'sha256-[^']*'", f"'sha256-{script_hash(page)}'", page, count=1)
    open(PAGE, "w", encoding="utf-8").write(new)
    print("CSP updated")

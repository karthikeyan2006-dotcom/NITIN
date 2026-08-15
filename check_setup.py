"""
check_setup.py  —  NITIN Pre-flight Diagnostics
================================================
Run this BEFORE starting app.py to verify everything is configured correctly.

Usage:
    python check_setup.py
"""

import sys, os

print("\n" + "="*60)
print("  NITIN — Pre-flight Check")
print("="*60 + "\n")

errors = []
warnings = []

# ── 1. Python version ──────────────────────────────────────────────────────
pv = sys.version_info
print(f"[1] Python version: {pv.major}.{pv.minor}.{pv.micro}", end="  ")
if pv.major < 3 or (pv.major == 3 and pv.minor < 9):
    print("⚠  Recommend Python 3.9+")
    warnings.append("Python < 3.9 detected")
else:
    print("✓")

# ── 2. Required packages ───────────────────────────────────────────────────
packages = {
    "flask":        "Flask",
    "flask_cors":   "flask-cors",
    "pymongo":      "pymongo",
    "bson":         "pymongo (bson is bundled)",
}
print("\n[2] Python packages:")
for mod, pkg in packages.items():
    try:
        __import__(mod)
        print(f"    {pkg:<30} ✓")
    except ImportError:
        print(f"    {pkg:<30} ✗  →  run: pip install {pkg}")
        errors.append(f"Missing package: {pkg}")

# ── 3. MongoDB connectivity ────────────────────────────────────────────────
print("\n[3] MongoDB connection:")
try:
    from pymongo import MongoClient
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    dbs = client.list_database_names()
    print(f"    mongodb://localhost:27017/  ✓  (databases: {', '.join(dbs[:5])})")

    # Check user_list database
    db = client["user_list"]
    user_count = db["users"].count_documents({})
    msg_count  = db["messages"].count_documents({})
    print(f"    user_list.users    : {user_count} document(s)")
    print(f"    user_list.messages : {msg_count} document(s)")
    client.close()
except Exception as e:
    print(f"    ✗  Cannot connect:  {e}")
    print("    → Make sure MongoDB is running:")
    print("      Windows : net start MongoDB   OR   mongod --dbpath C:\\data\\db")
    print("      Linux   : sudo systemctl start mongod")
    print("      macOS   : brew services start mongodb-community")
    errors.append("MongoDB not reachable")

# ── 4. arena_test.py ──────────────────────────────────────────────────────
print("\n[4] Pipeline (arena_test.py):")
sys.path.insert(0, os.path.dirname(__file__))
try:
    import arena_test as pipeline
    print("    arena_test.py        ✓")
    print(f"    Input folder  : {pipeline.CONFIG['input_folder']}")
    print(f"    Output folder : {pipeline.CONFIG['output_object_folder']}")
    # Check input folder exists
    from pathlib import Path
    if not Path(pipeline.CONFIG['input_folder']).exists():
        print(f"    ⚠  Input folder does not exist yet — will be created on first run")
        warnings.append("Input folder missing — create it before running pipeline")
except ImportError as e:
    print(f"    arena_test.py        ⚠  Not found ({e})")
    warnings.append("arena_test.py not in same directory — pipeline will be disabled")

# ── 5. Static files ────────────────────────────────────────────────────────
print("\n[5] Frontend files:")
for fname in ["static/login.html", "static/index.html"]:
    exists = os.path.exists(fname)
    print(f"    {fname:<35} {'✓' if exists else '✗ MISSING'}")
    if not exists:
        errors.append(f"Missing file: {fname}")

# ── 6. auth.py and messaging.py ───────────────────────────────────────────
print("\n[6] Module files:")
for fname in ["auth.py", "messaging.py", "app.py"]:
    exists = os.path.exists(fname)
    print(f"    {fname:<35} {'✓' if exists else '✗ MISSING'}")
    if not exists:
        errors.append(f"Missing file: {fname}")

# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + "="*60)
if errors:
    print(f"  ✗  {len(errors)} ERROR(S) — fix these before starting app.py:")
    for e in errors:
        print(f"     • {e}")
else:
    print("  ✓  All checks passed!")

if warnings:
    print(f"\n  ⚠  {len(warnings)} WARNING(S):")
    for w in warnings:
        print(f"     • {w}")

if not errors:
    print("\n  Ready to start:  python app.py")
print("="*60 + "\n")

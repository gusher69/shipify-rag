"""Preflight — the ONE command a developer runs on a brand-new machine
before starting development (2026-08-01 final configuration cleanup).

Order of checks (fails fast, cheapest/most-fundamental first):
  1. Python version
  2. Key dependencies importable (requirements.txt)
  3. .env file exists
  4. Full environment health check (scripts/validate_env.py — required
     variables, placeholders, OpenAI/Supabase/PostgreSQL/pgvector/Storage/
     Credential Store/LINE connectivity)

Composes scripts/validate_env.py rather than duplicating its logic — this
file owns only the things validate_env.py doesn't (interpreter version,
package installation, .env existence), then hands off to it for everything
about the configuration values themselves.

Usage:
    python scripts/preflight.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIN_PYTHON = (3, 13)

# name shown in the report -> the actual importable module name (differs
# for a few packages, e.g. python-dotenv -> dotenv)
_KEY_PACKAGES = {
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "openai": "openai",
    "supabase": "supabase",
    "python-dotenv": "dotenv",
    "cryptography": "cryptography",
    "psycopg2-binary": "psycopg2",
    "sentence-transformers": "sentence_transformers",
    "line-bot-sdk": "linebot",
}


def check_python_version():
    ok = sys.version_info[:2] >= _MIN_PYTHON
    got = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return ok, got


def check_dependencies():
    """Returns (missing, installed) — package display names."""
    import importlib
    missing, installed = [], []
    for display_name, module_name in _KEY_PACKAGES.items():
        try:
            importlib.import_module(module_name)
            installed.append(display_name)
        except ImportError:
            missing.append(display_name)
    return missing, installed


def check_env_file_exists():
    return (_REPO_ROOT / ".env").exists()


def _print_section(title):
    print(f"\n{'-' * 70}\n{title}\n{'-' * 70}")


def main() -> int:
    print("=" * 70)
    print("SHIPIFY AI PLATFORM — PREFLIGHT CHECK")
    print("=" * 70)

    blocking = False

    # 1. Python version
    _print_section("1. Python version")
    ok, got = check_python_version()
    required = ".".join(str(p) for p in _MIN_PYTHON)
    if ok:
        print(f"  [PASS] Python {got} (>= {required} required)")
    else:
        print(f"  [FAIL] Python {got} — this project requires >= {required}")
        blocking = True

    # 2. Dependencies
    _print_section("2. Dependencies (requirements.txt)")
    missing, installed = check_dependencies()
    print(f"  [PASS] {len(installed)} key package(s) importable: {', '.join(installed)}")
    if missing:
        print(f"  [FAIL] {len(missing)} missing: {', '.join(missing)}")
        print("         Fix: pip install -r requirements.txt")
        blocking = True

    # 3. .env exists
    _print_section("3. .env file")
    if check_env_file_exists():
        print("  [PASS] .env exists.")
    else:
        print("  [FAIL] .env does not exist.")
        print("         Fix: copy .env.setup.example to .env, then fill in every blank")
        print("         (see SETUP_NEW_MACHINE.md for the full walkthrough).")
        blocking = True
        # Can't meaningfully run the config/connectivity checks below without
        # a .env at all — config.py would just report every value as unset.
        print("\n" + "=" * 70)
        print("PREFLIGHT — FAIL (fix .env first, then re-run this command)")
        print("=" * 70)
        return 1

    # 4. Full environment health check (delegated — not duplicated)
    _print_section("4. Environment health check (scripts/validate_env.py)")
    from scripts.validate_env import run_all_checks, format_report
    results = run_all_checks()
    print(format_report(results))
    if any(r.status == "FAIL" for r in results):
        blocking = True

    print("\n" + "=" * 70)
    if blocking:
        print("PREFLIGHT — FAIL")
        print("Fix every [FAIL] line above, then re-run: python scripts/preflight.py")
    else:
        print("PREFLIGHT — PASS")
        print("Environment is healthy. Start the Admin app with:")
        print("  python -m uvicorn admin.routes:app --reload --port 8001")
        print("Then open http://localhost:8001/admin/login")
    print("=" * 70)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())

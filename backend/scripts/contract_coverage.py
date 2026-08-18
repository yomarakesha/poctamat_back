"""Say which contract operations this server answers, and which it does not.

Compares `postbox-contract/openapi.yaml` against the routes the application
actually registers. Path parameter names are ignored — `{booking_id}` and `{id}`
are the same route — so this reports missing operations, not naming drift.

    cd backend
    ./.venv/Scripts/python.exe scripts/contract_coverage.py [--all]

Without `--all` it reports only the client surface (the app's own tags), which
is what Plan 2c set out to make conform.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml  # noqa: E402

from app.main import API_PREFIX, app  # noqa: E402

CONTRACT = Path(__file__).resolve().parents[2] / "postbox-contract" / "openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}
CLIENT_TAGS = {
    "Auth (client)", "Profile", "Bookings", "Payments", "Access codes",
    # Cities, cell types, postamats and their availability: unauthenticated, and
    # the app reads them before anyone signs in.
    "Reference", "Webhooks",
}


def shape(path: str) -> str:
    """A route with its parameter names blanked, so only the shape matters."""
    return re.sub(r"\{[^}]+\}", "{}", path)


def ours() -> set[tuple[str, str]]:
    return {
        (shape(path.removeprefix(API_PREFIX)), method)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method in METHODS
    }


def main() -> int:
    everything = "--all" in sys.argv
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    mine = ours()

    have: list[str] = []
    missing: list[str] = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in METHODS:
                continue
            tags = set(operation.get("tags") or [])
            if not everything and not (tags & CLIENT_TAGS):
                continue
            line = f"{method.upper():6} {path:52} {operation.get('operationId', '')}"
            (have if (shape(path), method) in mine else missing).append(line)

    print(f"\nAnswered ({len(have)}):\n")
    for line in sorted(have):
        print(f"  {line}")
    print(f"\nNot built ({len(missing)}):\n")
    for line in sorted(missing):
        print(f"  {line}")
    print(f"\n{len(have)}/{len(have) + len(missing)} operations answered.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Throwaway: print each admin operation's request and success-response schema."""

import sys
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "postbox-contract" / "openapi.yaml")
    .read_text(encoding="utf-8")
)

WANT = sys.argv[1] if len(sys.argv) > 1 else "/admin"


def name(schema) -> str:
    if not isinstance(schema, dict):
        return str(schema)
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    if "allOf" in schema:
        return "allOf[" + ", ".join(name(part) for part in schema["allOf"]) + "]"
    if schema.get("type") == "array":
        return f"array<{name(schema.get('items', {}))}>"
    if schema.get("type") == "object" or "properties" in schema:
        return "{" + ", ".join(
            f"{key}: {name(value)}" for key, value in schema.get("properties", {}).items()
        ) + "}"
    return schema.get("type", "?")


for path, operations in SPEC["paths"].items():
    if WANT not in path:
        continue
    for method, operation in operations.items():
        if method not in {"get", "post", "patch", "put", "delete"}:
            continue
        body = (operation.get("requestBody", {}).get("content", {})
                .get("application/json", {}).get("schema"))
        print(f"{method.upper():6} {path}")
        if body:
            print(f"       in : {name(body)}")
        for status, response in operation.get("responses", {}).items():
            if not status.startswith("2"):
                continue
            schema = (response.get("content", {})
                      .get("application/json", {}).get("schema"))
            print(f"       {status}: {name(schema) if schema else '(no body)'}")

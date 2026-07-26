"""schema_check.py -- A small, dependency-free validator for exactly the
JSON Schema subset actually used by codemap/schema/trace-*.schema.json
(type, required, properties, additionalProperties, const, pattern,
minimum, items) -- not a general-purpose JSON Schema engine. The schema
files themselves stay plain, standard JSON Schema (draft-07) so any real
validator (the `jsonschema` PyPI package, an editor's built-in JSON Schema
support, a future CI step) can also use them directly; this module exists
so this repo's own zero-third-party-dependency rule (see requirements.txt)
doesn't force pulling one in just to check a trace file against its own
documented contract.
"""
import re

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "object": dict,
    "array": list,
    "boolean": bool,
}


def validate(value, schema, path="$") -> list:
    """Returns a list of human-readable error strings; empty if `value`
    conforms to `schema`."""
    errors = []

    if "const" in schema:
        if value != schema["const"]:
            errors.append(f"{path}: expected constant {schema['const']!r}, got {value!r}")
        return errors  # a const check is exhaustive on its own

    expected_type = schema.get("type")
    if expected_type:
        py_type = _TYPE_MAP.get(expected_type)
        if py_type and not isinstance(value, py_type):
            errors.append(f"{path}: expected type {expected_type}, got {type(value).__name__} ({value!r})")
            return errors  # further checks would be meaningless against the wrong type

    if expected_type == "string" and "pattern" in schema:
        if not re.match(schema["pattern"], value):
            errors.append(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")

    if expected_type == "integer" and "minimum" in schema:
        if value < schema["minimum"]:
            errors.append(f"{path}: {value!r} is below the minimum {schema['minimum']!r}")

    if expected_type == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property {key!r} (not in {sorted(properties)})")
        for key, sub_schema in properties.items():
            if key in value:
                errors.extend(validate(value[key], sub_schema, f"{path}.{key}"))

    if expected_type == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))

    return errors

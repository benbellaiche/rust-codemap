//! schema_check.rs -- Rust port of `src/codemap/schema_check.py`. A small
//! validator for exactly the JSON Schema subset actually used by
//! `src/codemap/schema/trace-*.schema.json` (type, required, properties,
//! additionalProperties, const, pattern, minimum, items) -- not a
//! general-purpose JSON Schema engine. See the Python original for the
//! full rationale.

use regex::Regex;
use serde_json::Value;

/// Returns a list of human-readable error strings; empty if `value`
/// conforms to `schema`.
pub fn validate(value: &Value, schema: &Value, path: &str) -> Vec<String> {
    let mut errors = Vec::new();

    if let Some(expected_const) = schema.get("const") {
        if value != expected_const {
            errors.push(format!(
                "{path}: expected constant {expected_const}, got {value}"
            ));
        }
        return errors; // a const check is exhaustive on its own
    }

    let expected_type = schema.get("type").and_then(Value::as_str);
    if let Some(expected_type) = expected_type {
        let matches = match expected_type {
            "string" => value.is_string(),
            "integer" => value.is_i64() || value.is_u64(),
            "number" => value.is_number(),
            "object" => value.is_object(),
            "array" => value.is_array(),
            "boolean" => value.is_boolean(),
            _ => true, // unknown declared type: nothing to check against
        };
        if !matches {
            errors.push(format!(
                "{path}: expected type {expected_type}, got {} ({value})",
                json_type_name(value)
            ));
            return errors; // further checks would be meaningless against the wrong type
        }
    }

    if expected_type == Some("string") {
        if let Some(pattern) = schema.get("pattern").and_then(Value::as_str) {
            let s = value.as_str().unwrap_or("");
            let re = Regex::new(pattern).unwrap();
            if !re.is_match(s) {
                errors.push(format!("{path}: {value} does not match pattern {pattern:?}"));
            }
        }
    }

    if expected_type == Some("integer") {
        if let Some(minimum) = schema.get("minimum").and_then(Value::as_i64) {
            let v = value.as_i64().unwrap_or(0);
            if v < minimum {
                errors.push(format!("{path}: {value} is below the minimum {minimum}"));
            }
        }
    }

    if expected_type == Some("object") {
        let empty_map = serde_json::Map::new();
        let properties = schema.get("properties").and_then(Value::as_object).unwrap_or(&empty_map);
        let obj = value.as_object().unwrap();
        if let Some(required) = schema.get("required").and_then(Value::as_array) {
            for key in required {
                let key = key.as_str().unwrap_or("");
                if !obj.contains_key(key) {
                    errors.push(format!("{path}: missing required property {key:?}"));
                }
            }
        }
        if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
            for key in obj.keys() {
                if !properties.contains_key(key) {
                    let mut allowed: Vec<&str> = properties.keys().map(String::as_str).collect();
                    allowed.sort_unstable();
                    errors.push(format!(
                        "{path}: unexpected property {key:?} (not in {allowed:?})"
                    ));
                }
            }
        }
        for (key, sub_schema) in properties {
            if let Some(v) = obj.get(key) {
                errors.extend(validate(v, sub_schema, &format!("{path}.{key}")));
            }
        }
    }

    if expected_type == Some("array") {
        if let Some(items_schema) = schema.get("items") {
            for (i, item) in value.as_array().unwrap().iter().enumerate() {
                errors.extend(validate(item, items_schema, &format!("{path}[{i}]")));
            }
        }
    }

    errors
}

fn json_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

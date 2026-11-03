from django.core.management import call_command


def test_openapi_schema_generates_without_warnings(tmp_path, db):
    """The OpenAPI document must stay valid and warning-free (fail-on-warn)."""
    out = tmp_path / "schema.yml"
    call_command("spectacular", "--file", str(out), "--validate", "--fail-on-warn")
    text = out.read_text()
    assert "/api/instances/{id}/snapshots" in text and "sessionAuth" in text

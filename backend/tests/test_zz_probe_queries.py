import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

pytestmark = pytest.mark.django_db


def test_probe(users, make_instance, client_for, roles, fake_agent):
    from apps.accounts.models import InstanceAccess
    insts = [make_instance() for _ in range(5)]
    for i in insts:
        InstanceAccess.objects.create(user=users["norole"], instance=i, role=roles["operator"])
    c = client_for(users["norole"])
    with CaptureQueriesContext(connection) as ctx:
        r = c.get("/api/instances/")
    assert r.status_code == 200 and len(r.json()) == 5
    for q in ctx.captured_queries:
        print(q["sql"][:200])
    print("TOTAL", len(ctx.captured_queries))

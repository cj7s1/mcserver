"""Django settings for the automated test suite.

pytest-django imports the settings module *before* any conftest.py is loaded, so
the test-mode switch has to be applied here rather than in conftest.py.
``MCPANEL_TESTING=1`` makes the main settings use a local-memory cache, the
in-memory channel layer, the fast MD5 password hasher and eager Celery tasks.
"""

from __future__ import annotations

import os

os.environ.setdefault("MCPANEL_TESTING", "1")

from mcpanel.settings import *  # noqa: F403
from mcpanel.settings import DATABASES

# Allow concurrent test runs (e.g. several developers/agents on one Postgres) to
# use separate throw-away databases.
DATABASES["default"]["TEST"] = {"NAME": os.environ.get("MCPANEL_TEST_DB_NAME", "test_mcpanel")}

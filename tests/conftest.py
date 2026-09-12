from fastapi.exception_handlers import http_exception_handler
from sqlalchemy.sql import Select
from sqlalchemy.sql.expression import Delete
from starlette.exceptions import HTTPException

from api.app import app


pytest_plugins = "tests.fixtures"

Select.__eq__ = Select.compare  # type: ignore
Delete.__eq__ = Delete.compare  # type: ignore

del app.user_middleware[0]  # remove db session for tests
app.exception_handlers[HTTPException] = http_exception_handler  # JSON errors without test-session rollback
app.middleware_stack = app.build_middleware_stack()

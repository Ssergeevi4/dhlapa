import pytest

from api.v1.routers.admin_audit import router


pytestmark = pytest.mark.no_db


def test_admin_audit_log_endpoint_requires_superadmin_role():
    route = next(
        item
        for item in router.routes
        if getattr(item, "path", None) == "/admin/audit-logs"
        and "GET" in getattr(item, "methods", set())
    )
    rbac_dependency = next(
        dependency
        for dependency in route.dependant.dependencies
        if dependency.name == "_"
    )
    closure_values = [
        cell.cell_contents
        for cell in (rbac_dependency.call.__closure__ or ())
    ]

    assert ["SuperAdmin"] in closure_values

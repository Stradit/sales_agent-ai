"""Base Celery Task Classes for Multi-Tenant Execution.

Enforces strict tenant scoping (§5.6):
- Requires client_slug in every task call or kwargs.
- Enforces fail-closed isolation using tenant_scope(client_slug).
- Prevents cross-tenant state pollution in distributed workers.
"""

from celery import Task
from db.tenant import tenant_scope, TenantContextError, validate_slug


class TenantTask(Task):
    """Abstract base Celery task that strictly requires and enforces tenant scope."""

    abstract = True

    def __call__(self, *args, **kwargs):
        client_slug = kwargs.get("client_slug")
        if not client_slug:
            # Check if passed as keyword argument
            raise TenantContextError("client_slug is required for executing TenantTask")

        clean_slug = validate_slug(client_slug)
        with tenant_scope(clean_slug):
            if getattr(self, "request_stack", None) is not None:
                return super().__call__(*args, **kwargs)
            return self.run(*args, **kwargs)

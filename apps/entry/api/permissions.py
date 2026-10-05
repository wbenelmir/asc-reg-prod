"""DRF permission classes for the device API."""

from __future__ import annotations

from rest_framework.permissions import BasePermission


class DenyAll(BasePermission):
    """The project-wide DRF default (`REST_FRAMEWORK`): an API view that does
    not declare its own permission explicitly is unreachable."""

    def has_permission(self, request, view) -> bool:
        return False

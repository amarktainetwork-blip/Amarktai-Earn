from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_GET


@require_GET
def landing_page(request):
    """Public product landing page; never exposes owner/runtime state."""
    return render(request, "control/landing.html")

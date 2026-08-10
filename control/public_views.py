from __future__ import annotations

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET


@require_GET
def landing_page(request):
    """Public product landing page; authenticated owners go straight to operations."""
    if getattr(request, "owner", None):
        return redirect("overview")
    return render(request, "control/landing.html")

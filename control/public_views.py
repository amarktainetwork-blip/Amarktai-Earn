from __future__ import annotations

from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from control.models import CommercialAPIProduct


@require_GET
def landing_page(request):
    """Public product landing page; authenticated owners go straight to operations."""
    if getattr(request, "owner", None):
        return redirect("overview")
    products = CommercialAPIProduct.objects.filter(
        enabled=True,
        proof_state__in=["ENGINEERING_PROVEN", "READY_FOR_PRODUCTION_PROOF"],
    ).order_by("slug")[:6]
    return render(request, "control/landing.html", {"commercial_products": products})


@require_GET
def terms_page(request):
    """Public terms and earnings disclaimer page."""
    return render(request, "control/terms.html")

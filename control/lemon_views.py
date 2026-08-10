from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from control.models import InboundOrder
from control.services.channel_ingress import ChannelIngressError
from control.services.inbound_controller import (
    InboundControllerError,
    accept_inbound_order,
    dispatch_accepted_inbound_orders,
)
from control.services.lemon_webhooks import dispatch_lemon_webhook


@csrf_exempt
@require_POST
def lemon_squeezy_webhook_api(request):
    try:
        result = dispatch_lemon_webhook(
            raw_body=request.body,
            signature=str(request.headers.get("X-Signature") or ""),
        )
        order_id = result.get("order_id")
        dispatch = None
        if result.get("handled") and order_id and not result.get("missing_buyer_inputs"):
            order = InboundOrder.objects.get(pk=order_id)
            if order.status == InboundOrder.Status.READY:
                accept_inbound_order(order.id, actor="lemon-squeezy", manual=False)
            dispatch = dispatch_accepted_inbound_orders(limit=20)
    except ChannelIngressError as exc:
        return JsonResponse({"error": exc.code}, status=exc.status)
    except (InboundControllerError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({**result, "dispatch": dispatch})

from .models import StockMovement


def erp_callback_alerts(request):
    user = request.user
    if not user.is_authenticated or not user.is_staff:
        return {"erp_callback_failed_count": 0}
    count = StockMovement.objects.filter(
        warehouse__owner=user,
        movement_type=StockMovement.Type.OUTBOUND,
        erp_callback_sent_at__isnull=True,
        erp_callback_attempts__gt=0,
    ).count()
    return {"erp_callback_failed_count": count}

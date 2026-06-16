from mailing.models import Client

ACTIVE_CLIENT_SESSION_KEY = "operator_active_client_id"


def operator_client_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated or not request.user.is_staff:
        return {}

    clients = Client.objects.select_related("organization").order_by("organization__slug", "slug")
    active_client = None
    active_client_id = request.session.get(ACTIVE_CLIENT_SESSION_KEY)
    if active_client_id:
        active_client = clients.filter(pk=active_client_id).first()
        if active_client is None:
            request.session.pop(ACTIVE_CLIENT_SESSION_KEY, None)
    else:
        first_client = clients.first()
        if first_client is not None and not clients.exclude(pk=first_client.pk).exists():
            request.session[ACTIVE_CLIENT_SESSION_KEY] = first_client.id
            active_client = first_client

    return {
        "operator_clients": clients,
        "operator_active_client": active_client,
        "operator_active_client_session_key": ACTIVE_CLIENT_SESSION_KEY,
    }

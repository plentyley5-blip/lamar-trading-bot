import datetime
import json
import urllib.parse
import urllib.request

from api.user_security import get_supabase_service_key, get_supabase_url


def has_active_subscription(user_id: str) -> bool:
    """Return True only when the user has a currently active, non-expired subscription."""
    url = get_supabase_url().rstrip("/")
    key = get_supabase_service_key().strip()
    if not url or not key:
        return False

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    query = urllib.parse.urlencode({
        "user_id": "eq." + str(user_id),
        "status": "eq.active",
        "expires_at": "gt." + now,
        "select": "id",
        "limit": "1",
    })
    request = urllib.request.Request(
        url + "/rest/v1/subscriptions?" + query,
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        return isinstance(data, list) and bool(data)
    except Exception:
        # Fail closed: if subscription verification cannot be completed,
        # do not allow a paid analysis to start.
        return False

"""Local deterministic smoke checks for the LM support handoff helper logic."""
import importlib.util
from pathlib import Path
import sys
import types

api = types.ModuleType("api")
security = types.ModuleType("api.user_security")
security.extract_bearer_token = lambda handler: "test"
security.get_supabase_service_key = lambda: "test-key"
security.get_supabase_url = lambda: "https://example.supabase.co"
security.verify_access_token = lambda token: {"id": "test-user"}
sys.modules["api"] = api
sys.modules["api.user_security"] = security

backend_path = Path(__file__).resolve().parent.parent / "handlers" / "support_chat.py"
spec = importlib.util.spec_from_file_location("support_chat_under_test", backend_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

state = {"status": "bot", "id": "conversation-1", "user_id": "customer-1"}
patch_calls = []
save_calls = []
alerts = []


def fake_supabase(method, table, query=None, payload=None, prefer=None):
    if method == "PATCH" and table == "support_conversations":
        patch_calls.append((method, table, query, payload))
        if state["status"] in {"bot", "resolved"}:
            state.update(payload)
            state["support_status"] = "waiting"
            return [dict(state)]
        return []
    if method == "GET" and table == "support_conversations":
        return [dict(state)]
    raise AssertionError((method, table, query, payload))


module.supabase_request = fake_supabase
module.save_message = lambda *args, **kwargs: save_calls.append(args) or {"id": "m1"}
module.alert_available_agents = lambda conversation: alerts.append(conversation["id"]) or {"available_agents": 4, "push_sent": 4}

first, delivery, created = module.set_waiting_for_human(dict(state), "smoke test")
assert created is True
assert first["support_status"] == "waiting"
assert delivery["push_sent"] == 4
assert len(alerts) == 1

second, duplicate_delivery, created_again = module.set_waiting_for_human(dict(first), "repeat request")
assert created_again is False
assert second["support_status"] == "waiting"
assert duplicate_delivery["push_sent"] == 0
assert len(alerts) == 1

# Available-agent capacity calculation: a single agent with a full slot is skipped.
module.supabase_request = lambda method, table, query=None, payload=None, prefer=None: (
    [{"user_id": "agent-a", "display_name": "A", "role": "agent", "max_active_chats": 1},
     {"user_id": "agent-b", "display_name": "B", "role": "agent", "max_active_chats": 2}]
    if table == "support_agents" else
    [{"id": "c1", "assigned_agent_id": "agent-a"}, {"id": "c2", "assigned_agent_id": "agent-b"}]
)
available = module.available_agents()
assert [a["user_id"] for a in available] == ["agent-b"]
print("Support handoff smoke checks passed.")

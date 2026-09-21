user = result.get("user")

if not isinstance(user, dict):
    user = {}

user_id = user.get("id")

# Supabase can return the user inside the response
# even when there is no active session.
if not user_id:
    return {
        "success": True,
        "message": (
            "Account created successfully. "
            "Please check your email to confirm your account."
        ),
        "user": {
            "id": "",
            "email": email,
            "full_name": full_name,
        },
        "access_token": None,
        "refresh_token": None,
        "session": None,
        "email_confirmation_required": True,
    }, 200

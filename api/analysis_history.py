import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler

from analysis_common import json_response

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    verify_access_token,
)


def _supabase_headers():
    key = get_supabase_service_key()

    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    }


def _load_history(user_id):
    query = (
        get_supabase_url()
        + "/rest/v1/analysis_jobs?"
        + "user_id=eq."
        + urllib.parse.quote(
            str(user_id),
            safe="",
        )
        + "&status=eq.completed"
        + "&select=id,instrument,trade_focus,created_at,openai_response_id"
        + "&order=created_at.desc"
        + "&limit=50"
    )

    request = urllib.request.Request(
        query,
        headers=_supabase_headers(),
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            raw = response.read().decode(
                "utf-8"
            )

            data = json.loads(
                raw
            )

    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        print(
            "Supabase history read failed:",
            raw,
        )

        raise RuntimeError(
            "History could not be loaded."
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError,
    ) as exc:
        raise RuntimeError(
            "Supabase is temporarily unavailable."
        ) from exc

    if not isinstance(
        data,
        list,
    ):
        return []

    history = []

    for row in data:

        if not isinstance(
            row,
            dict,
        ):
            continue

        encoded_result = str(
            row.get(
                "openai_response_id",
                "",
            )
        ).strip()

        if not encoded_result:
            continue

        try:
            decoded = (
                base64.urlsafe_b64decode(
                    encoded_result
                    + "="
                    * (
                        -len(encoded_result)
                        % 4
                    )
                )
                .decode("utf-8")
            )

            result = json.loads(
                decoded
            )

        except Exception:
            # Skip old rows which contain a real OpenAI
            # response ID instead of a stored completed result.
            continue

        if not isinstance(
            result,
            dict,
        ):
            continue

        instrument = str(
            row.get(
                "instrument",
                result.get(
                    "instrument",
                    "",
                ),
            )
        ).strip()

        # Compatibility with older queued rows that used:
        # EURUSD|job_nonce
        if "|" in instrument:
            instrument = instrument.split(
                "|",
                1,
            )[0].strip()

        result["instrument"] = (
            instrument
            or result.get(
                "instrument",
                "",
            )
        )

        history.append(
            {
                "job_id":
                    str(
                        row.get(
                            "id",
                            "",
                        )
                    ),

                "instrument":
                    result.get(
                        "instrument",
                        instrument,
                    ),

                "trade_focus":
                    str(
                        row.get(
                            "trade_focus",
                            result.get(
                                "trade_focus",
                                "",
                            ),
                        )
                    ),

                "created_at":
                    str(
                        row.get(
                            "created_at",
                            "",
                        )
                    ),

                "result":
                    result,
            }
        )

    return history


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        json_response(
            self,
            204,
            {},
        )

    def do_GET(self):
        try:
            access_token = extract_bearer_token(
                self
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user["id"]
            )

            history = _load_history(
                user_id
            )

            json_response(
                self,
                200,
                {
                    "history":
                        history,

                    "count":
                        len(history),
                },
            )

        except ValueError as exc:
            json_response(
                self,
                401,
                {
                    "error":
                        str(exc)
                },
            )

        except RuntimeError as exc:
            json_response(
                self,
                503,
                {
                    "error":
                        str(exc)
                },
            )

        except Exception as exc:
            print(
                "analysis_history error:",
                str(exc),
            )

            json_response(
                self,
                500,
                {
                    "error":
                        "History request failed."
                },
            )

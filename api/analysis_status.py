import base64
import json
import os
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

from api.user_security import (
    extract_bearer_token,
    get_supabase_service_key,
    get_supabase_url,
    release_analysis_slot,
    verify_access_token,
)

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    ""
).strip()

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/interactions"
)

GEMINI_API_REVISION = "2026-05-20"


def json_response(
    handler,
    status_code,
    payload
):
    body = json.dumps(
        payload,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(
        status_code
    )

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )

    handler.send_header(
        "Access-Control-Allow-Origin",
        "*"
    )

    handler.send_header(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization"
    )

    handler.send_header(
        "Access-Control-Allow-Methods",
        "GET, OPTIONS"
    )

    handler.send_header(
        "Cache-Control",
        "no-store, no-cache, must-revalidate"
    )

    handler.send_header(
        "Content-Length",
        str(len(body))
    )

    handler.end_headers()

    try:
        handler.wfile.write(body)
    except Exception:
        pass


def get_job_id(handler):
    parsed = urllib.parse.urlparse(
        handler.path
    )

    params = urllib.parse.parse_qs(
        parsed.query
    )

    return str(
        params.get(
            "job_id",
            [""]
        )[0]
    ).strip()


def get_job(
    job_id,
    user_id
):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
        + "?select="
        + "id,user_id,instrument,trade_focus,"
        + "status,openai_response_id,created_at,error_message"
        + "&id=eq."
        + urllib.parse.quote(
            job_id,
            safe=""
        )
        + "&user_id=eq."
        + urllib.parse.quote(
            user_id,
            safe=""
        )
        + "&limit=1"
    )

    key = get_supabase_service_key().strip()

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Supabase status error: "
            + detail[:2000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc

    try:
        rows = json.loads(
            raw
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Supabase returned invalid JSON."
        ) from exc

    if not isinstance(
        rows,
        list
    ) or not rows:
        return None

    return rows[0]


def get_gemini_interaction(
    interaction_id
):
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing from Vercel."
        )

    url = (
        GEMINI_URL.rstrip("/")
        + "/"
        + urllib.parse.quote(
            interaction_id,
            safe=""
        )
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
            "Api-Revision": GEMINI_API_REVISION
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        try:
            obj = json.loads(
                detail
            )

            message = (
                obj
                .get("error", {})
                .get("message")
                or detail[:2000]
            )

        except Exception:
            message = detail[:2000]

        raise RuntimeError(
            "Gemini status error: "
            + message
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Gemini connection error: "
            + str(exc)
        ) from exc

    try:
        return json.loads(
            raw
        )

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Gemini returned invalid status JSON."
        ) from exc


def extract_output_text(
    interaction
):
    direct = interaction.get(
        "output_text"
    )

    if (
        isinstance(
            direct,
            str
        )
        and direct.strip()
    ):
        return direct.strip()

    steps = interaction.get(
        "steps"
    )

    pieces = []

    if isinstance(
        steps,
        list
    ):
        for step in steps:

            if not isinstance(
                step,
                dict
            ):
                continue

            if step.get(
                "type"
            ) != "model_output":
                continue

            content = step.get(
                "content"
            )

            if not isinstance(
                content,
                list
            ):
                continue

            for item in content:

                if (
                    isinstance(
                        item,
                        dict
                    )
                    and item.get(
                        "type"
                    ) == "text"
                ):

                    value = item.get(
                        "text"
                    )

                    if isinstance(
                        value,
                        str
                    ):
                        pieces.append(
                            value
                        )

    return "\n".join(
        pieces
    ).strip()


def parse_result(
    text
):
    try:
        value = json.loads(
            text
        )

        if isinstance(
            value,
            dict
        ):
            return value

    except json.JSONDecodeError:
        pass

    cleaned = text.strip()

    if cleaned.startswith(
        "```"
    ):
        lines = cleaned.splitlines()

        if (
            lines
            and lines[0].strip().startswith("```")
        ):
            lines = lines[1:]

        if (
            lines
            and lines[-1].strip() == "```"
        ):
            lines = lines[:-1]

        cleaned = "\n".join(
            lines
        ).strip()

    if cleaned.lower().startswith(
        "json"
    ):
        cleaned = cleaned[4:].strip()

    try:
        value = json.loads(
            cleaned
        )

        if isinstance(
            value,
            dict
        ):
            return value

    except json.JSONDecodeError:
        pass

    start = cleaned.find(
        "{"
    )

    end = cleaned.rfind(
        "}"
    )

    if (
        start >= 0
        and end > start
    ):
        try:
            value = json.loads(
                cleaned[
                    start:end + 1
                ]
            )

            if isinstance(
                value,
                dict
            ):
                return value

        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        "Gemini returned invalid analysis JSON."
    )


def normalize_result(
    result,
    instrument
):
    signal = str(
        result.get(
            "signal",
            "NO TRADE"
        )
    ).strip().upper()

    if signal not in {
        "BUY",
        "SELL",
        "NO TRADE"
    }:
        signal = "NO TRADE"

    try:
        confidence = float(
            result.get(
                "confidence",
                0
            )
        )

    except Exception:
        confidence = 0

    confidence = max(
        0,
        min(
            100,
            confidence
        )
    )

    trend = str(
        result.get(
            "trend",
            "UNCLEAR"
        )
    ).strip().upper()

    if trend not in {
        "BULLISH",
        "BEARISH",
        "RANGE",
        "UNCLEAR"
    }:
        trend = "UNCLEAR"

    if signal == "BUY":
        idea = (
            "BUY "
            + instrument
        )

    elif signal == "SELL":
        idea = (
            "SELL "
            + instrument
        )

    else:
        idea = "NO TRADE"

    def text(name):
        value = result.get(
            name
        )

        if value is None:
            return "N/A"

        if isinstance(
            value,
            str
        ):
            return (
                value.strip()
                or "N/A"
            )

        return str(
            value
        )

    def list_value(name):
        value = result.get(
            name
        )

        if not isinstance(
            value,
            list
        ):
            return []

        return [
            str(x).strip()
            for x in value
            if str(x).strip()
        ]

    output = {
        "signal": signal,
        "confidence": confidence,
        "instrument": instrument,
        "trend": trend,
        "trade_idea": idea,
        "entry": text("entry"),
        "stop_loss": text("stop_loss"),
        "take_profit_1": text("take_profit_1"),
        "take_profit_2": text("take_profit_2"),
        "risk_reward": text("risk_reward"),
        "duration": text("duration"),
        "higher_timeframe_context": text(
            "higher_timeframe_context"
        ),
        "lower_timeframe_confirmation": text(
            "lower_timeframe_confirmation"
        ),
        "data_analysis": text(
            "data_analysis"
        ),
        "explanation": text(
            "explanation"
        ),
        "contributing_methods": list_value(
            "contributing_methods"
        ),
        "weak_methods": list_value(
            "weak_methods"
        ),
        "conflicting_methods": list_value(
            "conflicting_methods"
        ),
        "news_fundamental_risk": text(
            "news_fundamental_risk"
        ),
        "warnings": list_value(
            "warnings"
        )
    }

    if signal == "NO TRADE":
        output["entry"] = "N/A"
        output["stop_loss"] = "N/A"
        output["take_profit_1"] = "N/A"
        output["take_profit_2"] = "N/A"
        output["risk_reward"] = "N/A"

    return output


def encode_result(
    result
):
    raw = json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    return (
        base64.urlsafe_b64encode(
            raw
        )
        .decode("ascii")
        .rstrip("=")
    )


def decode_saved_result(
    encoded
):
    encoded = str(
        encoded or ""
    ).strip()

    if not encoded:
        raise RuntimeError(
            "The saved analysis result is empty."
        )

    try:
        raw = base64.urlsafe_b64decode(
            encoded
            + "=" * (
                -len(encoded) % 4
            )
        )

        value = json.loads(
            raw.decode("utf-8")
        )

    except Exception as exc:
        raise RuntimeError(
            "The saved analysis result is invalid."
        ) from exc

    if not isinstance(
        value,
        dict
    ):
        raise RuntimeError(
            "The saved analysis result is invalid."
        )

    return value


def update_job_completed(
    job_id,
    user_id,
    encoded_result
):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
        + "?id=eq."
        + urllib.parse.quote(
            job_id,
            safe=""
        )
        + "&user_id=eq."
        + urllib.parse.quote(
            user_id,
            safe=""
        )
        + "&status=eq.in_progress"
    )

    key = get_supabase_service_key().strip()

    payload = {
        "status": "completed",
        "openai_response_id": encoded_result
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8"),
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Supabase completion save error: "
            + detail[:2000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc

    try:
        rows = (
            json.loads(raw)
            if raw
            else []
        )

    except json.JSONDecodeError:
        rows = []

    return (
        isinstance(
            rows,
            list
        )
        and bool(rows)
    )


def update_job_failed(
    job_id,
    user_id,
    message
):
    url = (
        get_supabase_url().rstrip("/")
        + "/rest/v1/analysis_jobs"
        + "?id=eq."
        + urllib.parse.quote(
            job_id,
            safe=""
        )
        + "&user_id=eq."
        + urllib.parse.quote(
            user_id,
            safe=""
        )
        + "&status=eq.in_progress"
    )

    key = get_supabase_service_key().strip()

    payload = {
        "status": "failed",
        "error_message": str(
            message
        )[:4000]
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(
            payload,
            separators=(",", ":")
        ).encode("utf-8"),
        method="PATCH",
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "return=representation"
        }
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "Supabase failure save error: "
            + detail[:2000]
        ) from exc

    except (
        urllib.error.URLError,
        TimeoutError
    ) as exc:
        raise RuntimeError(
            "Supabase connection error: "
            + str(exc)
        ) from exc

    try:
        rows = (
            json.loads(raw)
            if raw
            else []
        )

    except json.JSONDecodeError:
        rows = []

    return (
        isinstance(
            rows,
            list
        )
        and bool(rows)
    )


class handler(
    BaseHTTPRequestHandler
):

    def do_OPTIONS(self):
        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, OPTIONS"
        )

        self.end_headers()

    def do_GET(self):
        try:
            access_token = (
                extract_bearer_token(
                    self
                )
            )

            user = verify_access_token(
                access_token
            )

            user_id = str(
                user.get(
                    "id",
                    ""
                )
            ).strip()

            if not user_id:
                raise ValueError(
                    "Authenticated user ID is missing."
                )

            job_id = get_job_id(
                self
            )

            if not job_id:
                json_response(
                    self,
                    400,
                    {
                        "error": "job_id is required."
                    }
                )
                return

            row = get_job(
                job_id,
                user_id
            )

            if row is None:
                json_response(
                    self,
                    404,
                    {
                        "status": "failed",
                        "error": "Analysis job not found."
                    }
                )
                return

            status = str(
                row.get(
                    "status",
                    ""
                )
            ).strip().lower()

            if status == "completed":
                result = decode_saved_result(
                    row.get(
                        "openai_response_id"
                    )
                )

                json_response(
                    self,
                    200,
                    {
                        "status": "completed",
                        "job_id": row.get("id"),
                        "result": result
                    }
                )

                return

            if status == "failed":
                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": row.get("id"),
                        "error": (
                            row.get(
                                "error_message"
                            )
                            or "The analysis failed."
                        )
                    }
                )

                return

            interaction_id = str(
                row.get(
                    "openai_response_id",
                    ""
                )
            ).strip()

            if not interaction_id:
                json_response(
                    self,
                    500,
                    {
                        "status": "failed",
                        "error": (
                            "Gemini interaction ID is missing."
                        )
                    }
                )

                return

            interaction = (
                get_gemini_interaction(
                    interaction_id
                )
            )

            interaction_status = str(
                interaction.get(
                    "status",
                    "in_progress"
                )
            ).strip().lower()

            if interaction_status in {
                "in_progress",
                "queued",
                "processing"
            }:
                json_response(
                    self,
                    200,
                    {
                        "status": "in_progress",
                        "poll_after_seconds": 2
                    }
                )

                return

            if interaction_status == "completed":
                text = extract_output_text(
                    interaction
                )

                if not text:
                    raise RuntimeError(
                        "Gemini completed the interaction "
                        "but returned no text."
                    )

                result = parse_result(
                    text
                )

                result = normalize_result(
                    result,
                    str(
                        row.get(
                            "instrument",
                            ""
                        )
                    ).strip()
                )

                encoded = encode_result(
                    result
                )

                first_writer = (
                    update_job_completed(
                        job_id,
                        user_id,
                        encoded
                    )
                )

                if first_writer:
                    json_response(
                        self,
                        200,
                        {
                            "status": "completed",
                            "job_id": job_id,
                            "result": result
                        }
                    )

                    return

                current = get_job(
                    job_id,
                    user_id
                )

                if (
                    current
                    and str(
                        current.get(
                            "status",
                            ""
                        )
                    ).lower()
                    == "completed"
                ):
                    saved = decode_saved_result(
                        current.get(
                            "openai_response_id"
                        )
                    )

                    json_response(
                        self,
                        200,
                        {
                            "status": "completed",
                            "job_id": job_id,
                            "result": saved
                        }
                    )

                    return

                raise RuntimeError(
                    "The analysis completed "
                    "but could not be saved."
                )

            if interaction_status in {
                "failed",
                "cancelled",
                "expired",
                "incomplete"
            }:
                message = (
                    "Gemini analysis failed."
                )

                err = interaction.get(
                    "error"
                )

                if isinstance(
                    err,
                    dict
                ):
                    message = str(
                        err.get(
                            "message"
                        )
                        or message
                    )

                elif (
                    isinstance(
                        err,
                        str
                    )
                    and err.strip()
                ):
                    message = err.strip()

                first_writer = (
                    update_job_failed(
                        job_id,
                        user_id,
                        message
                    )
                )

                if first_writer:
                    try:
                        release_analysis_slot(
                            user_id
                        )
                    except Exception:
                        pass

                json_response(
                    self,
                    200,
                    {
                        "status": "failed",
                        "job_id": job_id,
                        "error": message
                    }
                )

                return

            json_response(
                self,
                200,
                {
                    "status": "in_progress",
                    "poll_after_seconds": 2
                }
            )

        except ValueError as exc:
            json_response(
                self,
                401,
                {
                    "error": str(exc)
                }
            )

        except RuntimeError as exc:
            json_response(
                self,
                502,
                {
                    "error": str(exc)
                }
            )

        except Exception as exc:
            json_response(
                self,
                500,
                {
                    "error": (
                        "Analysis status backend error: "
                        + str(exc)
                    )
                }
            )

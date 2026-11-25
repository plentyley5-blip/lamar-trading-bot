"""Recent geopolitical news context for LM Analyzer High Impact News.

Uses GDELT DOC 2.0 headlines, caches results in the existing private Supabase
shared cache, and never treats uncorroborated/stale headlines as a trade factor.
Only article metadata is fetched here; the model is explicitly told that full
article text has not been independently reviewed.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from service_cache import (
    SharedCacheError,
    claim_shared_cache_refresh,
    read_shared_cache,
    release_shared_cache_refresh,
    write_shared_cache,
)

CACHE_TTL_SECONDS = 900
STALE_MAX_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 6
MAX_SOURCE_ARTICLES = 50
MAX_DISPLAY_ARTICLES = 12

# GDELT searches English-language translations across international coverage.
# Each region block is ANDed with the geopolitical-topic block.
CURRENCY_REGIONS = {
    "USD": ("United States", '("United States" OR US OR American OR Washington)'),
    "EUR": ("Euro area", '("European Union" OR EU OR European OR Brussels OR ECB)'),
    "GBP": ("United Kingdom", '("United Kingdom" OR UK OR Britain OR British OR London)'),
    "JPY": ("Japan", '(Japan OR Japanese OR Tokyo)'),
    "CHF": ("Switzerland", '(Switzerland OR Swiss OR Bern)'),
    "CAD": ("Canada", '(Canada OR Canadian OR Ottawa)'),
    "AUD": ("Australia", '(Australia OR Australian OR Canberra)'),
    "NZD": ("New Zealand", '("New Zealand" OR NZ OR Wellington)'),
}

GEOPOLITICAL_TERMS = (
    '(conflict OR war OR sanctions OR tariff OR tariffs OR "trade restrictions" '
    'OR military OR "military strike" OR attack OR ceasefire OR "shipping disruption" '
    'OR "energy supply" OR election OR tensions OR geopolitical OR blockade OR escalation)'
)

# A conservative publisher/institution allow-list. A matching domain is not
# treated as proof of truth; it only qualifies as a source for corroboration.
TRUSTED_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "ft.com",
    "bloomberg.com", "wsj.com", "nytimes.com", "theguardian.com",
    "cnn.com", "cnbc.com", "abc.net.au", "abcnews.go.com", "npr.org",
    "aljazeera.com", "france24.com", "dw.com", "euronews.com",
    "politico.eu", "politico.com", "thehill.com", "pbs.org",
    "sky.com", "skynews.com", "cbc.ca", "ctvnews.ca",
    "japantimes.co.jp", "nhk.or.jp", "asahi.com", "smh.com.au",
    "nzherald.co.nz", "rfi.fr",
    "un.org", "nato.int", "europa.eu", "state.gov", "whitehouse.gov",
    "gov.uk", "ec.europa.eu", "consilium.europa.eu", "bis.org",
    "imf.org", "opec.org", "eia.gov", "treasury.gov", "defense.gov",
    "mofa.go.jp", "international.gc.ca", "dfat.gov.au", "mfat.govt.nz",
}


def _now():
    return datetime.now(timezone.utc)


def _parse_gdelt_time(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _domain_is_trusted(domain):
    value = str(domain or "").strip().lower().rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    return any(value == item or value.endswith("." + item) for item in TRUSTED_DOMAINS)


def _article_domain(article):
    # Trust the actual URL host, not a separate provider-supplied domain field.
    # Reject metadata/URL mismatches rather than accepting a spoofed publisher.
    try:
        host = (urlsplit(str(article.get("url") or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    declared = str(article.get("domain") or "").strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if declared.startswith("www."):
        declared = declared[4:]
    if not host:
        return ""
    if declared and not (host == declared or host.endswith("." + declared)):
        return ""
    return host


def build_gdelt_query(currency):
    region = CURRENCY_REGIONS.get(str(currency or "").upper())
    if not region:
        return None
    return region[1] + " " + GEOPOLITICAL_TERMS


def _normalise_articles(raw_articles):
    now = _now()
    earliest = now - timedelta(hours=72)
    rows = []
    seen = set()
    for item in raw_articles if isinstance(raw_articles, list) else []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:220]
        url = str(item.get("url") or "").strip()
        domain = _article_domain(item)
        published = _parse_gdelt_time(item.get("seendate"))
        if not title or not url.startswith(("https://", "http://")):
            continue
        if not _domain_is_trusted(domain) or published is None:
            continue
        if published < earliest or published > now + timedelta(minutes=5):
            continue
        key = (title.casefold(), domain)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "title": title,
            "source": domain,
            "published_at": published.isoformat().replace("+00:00", "Z"),
            "url": url[:800],
            "source_country": str(item.get("sourcecountry") or "")[:80],
            "language": str(item.get("language") or "")[:24],
        })
    rows.sort(key=lambda x: x["published_at"], reverse=True)
    return rows[:MAX_DISPLAY_ARTICLES]


def _fetch(currency):
    region = CURRENCY_REGIONS[str(currency).upper()][0]
    query = build_gdelt_query(currency)
    params = urllib.parse.urlencode({
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(MAX_SOURCE_ARTICLES),
        "timespan": "3d",
        "sort": "DateDesc",
    })
    request = urllib.request.Request(
        "https://api.gdeltproject.org/api/v2/doc/doc?" + params,
        headers={
            "Accept": "application/json",
            "User-Agent": "LMAnalyzer/1.0 geopolitical-context",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(1_500_001)
            if len(raw) > 1_500_000:
                raise RuntimeError("Geopolitical news response was unexpectedly large.")
            data = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("The geopolitical news provider is rate-limiting requests.") from exc
        raise RuntimeError("The geopolitical news provider returned HTTP {}.".format(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("The geopolitical news provider is temporarily unavailable.") from exc

    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list):
        raise RuntimeError("The geopolitical news provider returned an unexpected response.")
    normalised = _normalise_articles(articles)
    domains = sorted({x["source"] for x in normalised})
    return {
        "provider": "GDELT DOC 2.0",
        "region": region,
        "status": "AVAILABLE" if normalised else "NO_VERIFIED_COVERAGE",
        "checked_at": _now().isoformat().replace("+00:00", "Z"),
        "window_hours": 72,
        "independent_source_count": len(domains),
        "source_domains": domains,
        "corroborated": len(domains) >= 2,
        "articles": normalised,
        "source_note": (
            "Recent article headlines and metadata only. The full article text has not been fetched "
            "or independently fact-checked; headlines are context, not verified conclusions."
        ),
    }


def _unavailable(currency, reason):
    region = CURRENCY_REGIONS.get(str(currency or "").upper(), ("Unknown region", ""))[0]
    return {
        "provider": "GDELT DOC 2.0",
        "region": region,
        "status": "UNAVAILABLE",
        "checked_at": _now().isoformat().replace("+00:00", "Z"),
        "window_hours": 72,
        "independent_source_count": 0,
        "source_domains": [],
        "corroborated": False,
        "articles": [],
        "source_note": str(reason or "Recent geopolitical context is unavailable.")[:200],
    }


def get_geopolitical_context(currency):
    """Return current/stale/unavailable context without blocking trade analysis."""
    code = str(currency or "").upper()
    if code not in CURRENCY_REGIONS:
        return _unavailable(code, "No geopolitical region mapping exists for this event currency.")
    cache_key = "lm_geopolitical_news_" + code
    cached_payload = None
    cache_age = None
    try:
        cached = read_shared_cache(cache_key)
        candidate = cached.get("payload") if isinstance(cached, dict) else None
        if isinstance(candidate, dict) and candidate.get("provider") == "GDELT DOC 2.0":
            cached_payload = candidate
            stamp = _parse_gdelt_time(cached.get("updated_at"))
            if stamp is None:
                try:
                    stamp = datetime.fromisoformat(
                        str(cached.get("updated_at") or "").replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except Exception:
                    stamp = None
            if stamp is not None:
                cache_age = max(0, int((_now() - stamp).total_seconds()))
    except Exception:
        cached_payload = None

    if cached_payload is not None and cache_age is not None and cache_age <= CACHE_TTL_SECONDS:
        result = dict(cached_payload)
        result["cache_status"] = "FRESH"
        result["cache_age_seconds"] = cache_age
        return result

    try:
        claimed = claim_shared_cache_refresh(cache_key, lock_seconds=20)
    except Exception as exc:
        if cached_payload is not None and cache_age is not None and cache_age <= STALE_MAX_SECONDS:
            claimed = False
        else:
            return _unavailable(code, "Shared geopolitical cache is unavailable: " + str(exc)[:120])

    if claimed:
        try:
            result = _fetch(code)
            write_shared_cache(cache_key, result)
            result = dict(result)
            result["cache_status"] = "FRESH"
            result["cache_age_seconds"] = 0
            return result
        except Exception as exc:
            release_shared_cache_refresh(cache_key)
            if cached_payload is not None and cache_age is not None and cache_age <= STALE_MAX_SECONDS:
                result = dict(cached_payload)
                result["status"] = "STALE"
                result["corroborated"] = False
                result["cache_status"] = "STALE_FALLBACK"
                result["cache_age_seconds"] = cache_age
                result["source_note"] = (
                    "Cached headlines are stale and were NOT counted as trade confirmation. "
                    + str(exc)[:120]
                )
                return result
            return _unavailable(code, str(exc))

    if cached_payload is not None and cache_age is not None and cache_age <= STALE_MAX_SECONDS:
        result = dict(cached_payload)
        result["status"] = "STALE"
        result["corroborated"] = False
        result["cache_status"] = "STALE_FALLBACK"
        result["cache_age_seconds"] = cache_age
        result["source_note"] = (
            "Cached headlines are stale and were NOT counted as trade confirmation while a refresh is in progress."
        )
        return result

    return _unavailable(code, "A current geopolitical-news refresh is already running. Retry shortly.")

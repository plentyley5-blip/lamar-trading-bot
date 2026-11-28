const TEXT_BODY = `https://lmanalyzer.pages.dev/
https://lmanalyzer.pages.dev/about/
`;

function headers() {
  return {
    "Content-Type": "text/plain; charset=utf-8",
    "Cache-Control": "public, max-age=3600, s-maxage=3600",
    "X-Content-Type-Options": "nosniff"
  };
}

export async function onRequestGet() {
  return new Response(TEXT_BODY, { status: 200, headers: headers() });
}

export async function onRequestHead() {
  return new Response(null, { status: 200, headers: headers() });
}

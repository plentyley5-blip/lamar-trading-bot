const XML_BODY = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://lmanalyzer.pages.dev/</loc>
  </url>
  <url>
    <loc>https://lmanalyzer.pages.dev/about/</loc>
  </url>
</urlset>
`;

const RESPONSE_HEADERS = {
  "Content-Type": "application/xml; charset=utf-8",
  "Cache-Control": "public, max-age=300, s-maxage=300",
  "X-Content-Type-Options": "nosniff"
};

export async function onRequestGet() {
  return new Response(XML_BODY, { status: 200, headers: RESPONSE_HEADERS });
}

export async function onRequestHead() {
  return new Response(null, { status: 200, headers: RESPONSE_HEADERS });
}

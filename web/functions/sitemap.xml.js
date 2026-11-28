const XML_BODY = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://lmanalyzer.pages.dev/</loc>
    <lastmod>${new Date().toISOString().slice(0, 10)}</lastmod>
  </url>
  <url>
    <loc>https://lmanalyzer.pages.dev/about/</loc>
    <lastmod>${new Date().toISOString().slice(0, 10)}</lastmod>
  </url>
</urlset>
`;

function headers() {
  return {
    "Content-Type": "application/xml; charset=utf-8",
    "Cache-Control": "public, max-age=3600, s-maxage=3600",
    "X-Content-Type-Options": "nosniff"
  };
}

export async function onRequestGet() {
  return new Response(XML_BODY, { status: 200, headers: headers() });
}

export async function onRequestHead() {
  return new Response(null, { status: 200, headers: headers() });
}

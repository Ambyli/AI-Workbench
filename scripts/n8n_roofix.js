// Find the Roofix proposal URL for the interceptor. Order: the direct
// roofix.io/project/<id> link in the subject, text body, then HTML body; the
// tokenized url<N>.roofix.io/ls/click link (HTML, then text) only as a fallback.
// The url<N>.roofix.io/wf/open link is the open-tracking pixel — never used.
//
// The email is read from the item's `result` object ('Async AI Analysis'
// output) or, failing that, from the item itself. Set SOURCE_NODE only if the
// email lives on an earlier node instead (e.g. the Gmail node's name).
const SOURCE_NODE = "";

const item = (SOURCE_NODE ? $(SOURCE_NODE).item.json : $json) ?? {};
const src =
  item.result && typeof item.result === "object" ? item.result : item;

// "]" is excluded so a bracketed plain-text link doesn't swallow its closer.
const TRACKING_URL_RE = /https?:\/\/url\d+\.roofix\.io\/ls\/click\?[^\s"'<>\[\]]+/i;
// Any roofix.io subdomain (app., www.); a Bubble id (<digits>x<digits>) or a
// UUID. Stops before the query string so the project opens on its default tab.
const PROJECT_URL_RE =
  /https?:\/\/(?:[a-z0-9-]+\.)?roofix\.io\/project\/([0-9]+x[0-9]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i;
const BUBBLE_ID_RE = /^[0-9]+x[0-9]+$/i;

// First non-empty field among the names the bridge and n8n's Gmail node use.
const pick = (...keys) => {
  for (const k of keys) {
    if (typeof src[k] === "string" && src[k]) return src[k];
  }
  return "";
};

// Python's html.unescape, close enough for email subjects and bodies.
const unescapeHtml = (s) =>
  s
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) =>
      String.fromCodePoint(parseInt(h, 16)),
    )
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&");

// The bridge does not unescape the HTML body before matching; neither do we.
const rawHtml = pick("body_html", "raw_html", "html", "textAsHtml");
const body = unescapeHtml(pick("body_text", "body", "text", "snippet"));
const subject = unescapeHtml(pick("subject", "Subject"));

const candidates = [
  ["subject", PROJECT_URL_RE, subject],
  ["body_text", PROJECT_URL_RE, body],
  ["body_html", PROJECT_URL_RE, rawHtml],
  ["body_html", TRACKING_URL_RE, rawHtml],
  ["body_text", TRACKING_URL_RE, body],
];

let trackingUrl = null;
let trackingUrlSource = null;
let urlId = null;
for (const [source, re, text] of candidates) {
  const m = text.match(re);
  if (m) {
    trackingUrl = m[0];
    trackingUrlSource = source;
    urlId = re === PROJECT_URL_RE ? m[1] : null;
    break;
  }
}

// roofix_id is what Phoenix's migration_external_id holds: the Bubble id. A
// UUID from an app.roofix.io link isn't known to be the same id, so it is kept
// apart as roofix_url_id and roofix_id stays null until a scrape supplies it.
return {
  json: {
    ...$json,
    roofix: {
      tracking_url: trackingUrl,
      tracking_url_source: trackingUrlSource,
      roofix_url_id: urlId,
      roofix_id: urlId && BUBBLE_ID_RE.test(urlId) ? urlId : null,
    },
  },
};

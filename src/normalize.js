// Normalize + validate Gumroad sales for Airtable.
// Works for both inputs:
//   - Gumroad Ping (Webhook node item: { body: {...form fields...}, headers, query })
//   - Gumroad API  (one sale object from GET /v2/sales, after "Split Out")
// Output keys are EXACT Airtable column names (Airtable node uses auto-mapping).
// Keys starting with "_" are control fields and are ignored by the Airtable node.
//
// Dedup strategy:
//   1) Airtable "Create or Update" merges on "Sale ID"  -> one row per sale, ever.
//   2) Duplicates inside one run (overlapping API pages, repeated pings) are merged here.
//   3) Refund pings carry the SAME sale_id -> they update the existing row (Refunded = true).
//      "Refunded"/"Disputed" are only written when true, so a late or retried "sale" ping
//      can never un-refund a row.

const SKIP_TEST_SALES = false; // true = drop test pings (your own purchases, "Send test ping")
const ZERO_DECIMAL = ['jpy', 'krw']; // API amounts in these currencies have no minor units
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const str = (v) => (v === undefined || v === null ? '' : String(v).trim());
const toBool = (v) => v === true || v === 1 || ['true', '1', 'yes'].includes(str(v).toLowerCase());
const toInt = (v) => (/^-?\d+$/.test(str(v)) ? parseInt(str(v), 10) : NaN);

// Gumroad posts x-www-form-urlencoded. Nested hashes arrive as flat keys: "variants[Size]=L".
// Accepts a nested object, flat bracket keys, or a plain string.
function collect(src, name) {
  const v = src[name];
  if (v && typeof v === 'object' && !Array.isArray(v)) return v;
  const out = {};
  const prefix = name + '[';
  for (const k of Object.keys(src)) {
    if (k.startsWith(prefix) && k.endsWith(']')) out[k.slice(prefix.length, -1)] = src[k];
  }
  if (Object.keys(out).length) return out;
  return str(v) ? { value: str(v) } : {};
}
const dictToText = (d) =>
  Object.entries(d)
    .filter(([, v]) => str(v) !== '')
    .map(([k, v]) => (k === 'value' ? str(v) : `${k}: ${str(v)}`))
    .join(', ');

function normalizeOne(json) {
  const isPing = json && typeof json.body === 'object' && json.body !== null;
  const src = isPing ? json.body : json || {};
  const errors = [];

  const pingFormat = isPing || src.sale_id !== undefined;
  let saleId, ts, cents, currency, orderNumber, permalink, event, refunded, disputed, test, offer, country;
  if (pingFormat) {
    // Ping format (also used by the local test runner without a "body" wrapper)
    saleId = str(src.sale_id);
    ts = str(src.sale_timestamp);
    cents = toInt(src.price); // Ping: ALWAYS USD cents, whatever "currency" says
    currency = 'USD';
    orderNumber = toInt(src.order_number);
    permalink = str(src.product_permalink);
    event = str(src.resource_name) || 'sale';
    refunded = toBool(src.refunded) || event === 'refund';
    disputed = toBool(src.disputed) || event === 'dispute';
    test = toBool(src.test);
    offer = str(src.offer_code);
    country = str(src.ip_country);
  } else {
    // API format (GET /v2/sales): price is in the sale currency's minor units
    saleId = str(src.id);
    ts = str(src.created_at);
    currency = str(src.currency).toUpperCase() || 'USD';
    cents = toInt(src.price);
    orderNumber = toInt(src.order_id);
    permalink = str(src.product_permalink);
    event = 'api-sync';
    refunded = toBool(src.refunded);
    disputed = toBool(src.disputed) || toBool(src.chargedback);
    offer = src.offer_code && typeof src.offer_code === 'object' ? str(src.offer_code.code || src.offer_code.name) : str(src.offer_code);
    country = str(src.country || src.ip_country);
  }

  const email = str(src.email || src.purchase_email).toLowerCase();
  const productId = str(src.product_id);
  const date = new Date(ts);

  if (!saleId) errors.push('missing sale_id');
  if (!EMAIL_RE.test(email)) errors.push(`invalid email "${email}"`);
  if (!ts || isNaN(date.getTime())) errors.push(`invalid sale timestamp "${ts}"`);
  if (isNaN(cents) || cents < 0) errors.push(`invalid price "${str(src.price)}"`);
  if (!productId) errors.push('missing product_id');

  const divisor = ZERO_DECIMAL.includes(currency.toLowerCase()) ? 1 : 100;
  const quantity = toInt(src.quantity);

  const rec = {
    'Sale ID': saleId,
    'Order Number': isNaN(orderNumber) ? undefined : orderNumber,
    'Sale Date': errors.some((e) => e.startsWith('invalid sale timestamp')) ? undefined : date.toISOString(),
    Email: email,
    'Full Name': str(src.full_name),
    'Product Name': str(src.product_name),
    'Product ID': productId,
    'Product Permalink': permalink,
    Amount: isNaN(cents) ? undefined : Math.round((cents / divisor) * 100) / 100,
    Currency: currency,
    Quantity: isNaN(quantity) ? undefined : quantity,
    Variants: dictToText(collect(src, 'variants')),
    'Offer Code': offer,
    Country: country,
    'Can Contact': toBool(src.can_contact),
    'Subscription ID': str(src.subscription_id),
    Test: pingFormat ? test : undefined, // the API has no test flag: never overwrite the ping's value
    Source: pingFormat ? 'Gumroad Ping' : 'Gumroad API',
    'Last Event': event,
    'Synced At': new Date().toISOString(),
  };
  if (refunded) rec.Refunded = true;
  if (disputed) rec.Disputed = true;

  // Drop empty values so an update never blanks out a column
  for (const k of Object.keys(rec)) if (rec[k] === undefined || rec[k] === '') delete rec[k];

  rec._valid = errors.length === 0;
  rec._errors = errors.join('; ');
  return rec;
}

function normalizeAll(items) {
  const bySale = new Map();
  const out = [];
  for (const item of items) {
    const rec = normalizeOne(item.json);
    if (SKIP_TEST_SALES && rec.Test) continue;
    if (!rec._valid) {
      out.push(rec);
      continue;
    }
    const prev = bySale.get(rec['Sale ID']);
    if (prev) {
      // same sale twice in one run: keep the newest data, never lose a refund/dispute flag
      const merged = { ...prev, ...rec };
      if (prev.Refunded || rec.Refunded) merged.Refunded = true;
      if (prev.Disputed || rec.Disputed) merged.Disputed = true;
      bySale.set(rec['Sale ID'], merged);
    } else {
      bySale.set(rec['Sale ID'], rec);
    }
  }
  return [...bySale.values(), ...out];
}

// n8n Code node entry point ("Run Once for All Items")
return normalizeAll($input.all()).map((json) => ({ json }));

/**
 * Cloudflare Worker — Discord `/hunt` slash-command relay for Nellis Hunter.
 *
 * Discord posts every interaction to this Worker's URL. The Worker must:
 *   1. Verify the Ed25519 request signature (Discord rejects you otherwise).
 *   2. Answer a PING with a PONG (Discord's endpoint health check).
 *   3. For /hunt: reply within 3s with a deferred ack ("Hunter is searching…"),
 *      then asynchronously trigger the GitHub Actions workflow that does the
 *      actual scrape and posts results back to the interaction.
 *
 * No scraping happens here — the Worker is a thin, stateless relay. The heavy
 * work runs in GitHub Actions (free), so there is no always-on server.
 *
 * Auth to GitHub is via a GitHub App (a machine identity, not a personal token):
 * the Worker signs a short-lived JWT with the App's private key, exchanges it for
 * a ~1h installation token, and uses that to dispatch the workflow. Installation
 * tokens auto-rotate, so nothing here ever expires from a human's perspective.
 *
 * Required secrets (set via `wrangler secret put <NAME>`):
 *   DISCORD_PUBLIC_KEY   — app's Public Key (Discord dev portal → General Info)
 *   GH_APP_PRIVATE_KEY   — GitHub App private key, PKCS#8 PEM ("BEGIN PRIVATE KEY")
 * Required vars (in wrangler.toml [vars]):
 *   GH_REPO              — e.g. "grontathan/nellis-hunter"
 *   GH_REF               — branch to run the workflow on, e.g. "main"
 *   GH_APP_ID            — the GitHub App's App ID (not a secret)
 *   GH_INSTALLATION_ID   — the App's installation id on the repo (not a secret)
 */

const WORKFLOW_FILE = "hunt-on-demand.yml";

// Discord interaction + response type enums (the ones we use).
const InteractionType = { PING: 1, APPLICATION_COMMAND: 2 };
const InteractionResponseType = { PONG: 1, DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5 };

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return bytes;
}

async function verifySignature(request, rawBody, publicKey) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    hexToBytes(publicKey),
    { name: "Ed25519", namedCurve: "Ed25519" },
    false,
    ["verify"],
  );
  return crypto.subtle.verify(
    { name: "Ed25519" },
    key,
    hexToBytes(signature),
    new TextEncoder().encode(timestamp + rawBody),
  );
}

function optionValue(options, name) {
  const opt = (options || []).find((o) => o.name === name);
  return opt ? opt.value : undefined;
}

// base64url-encode a byte buffer (JWT segments + signature use this, not plain b64).
function b64url(bytes) {
  const arr = new Uint8Array(bytes);
  let bin = "";
  for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Decode a PKCS#8 PEM ("BEGIN PRIVATE KEY") into the DER bytes importKey wants.
function pemToArrayBuffer(pem) {
  const b64 = pem
    .replace(/-----BEGIN [^-]+-----/, "")
    .replace(/-----END [^-]+-----/, "")
    .replace(/\s+/g, "");
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

// Build and RS256-sign a GitHub App JWT (max 10-min lifetime; iat backdated for clock skew).
async function appJwt(env) {
  const now = Math.floor(Date.now() / 1000);
  const enc = new TextEncoder();
  const header = b64url(enc.encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const payload = b64url(
    enc.encode(JSON.stringify({ iat: now - 60, exp: now + 540, iss: env.GH_APP_ID })),
  );
  const data = `${header}.${payload}`;
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToArrayBuffer(env.GH_APP_PRIVATE_KEY),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc.encode(data));
  return `${data}.${b64url(sig)}`;
}

// Cache the installation token across requests in the same isolate (it lasts ~1h)
// so we skip the JWT + exchange round-trip on most hunts. Best-effort only.
let cachedToken = null; // { token, exp } — exp is unix seconds

async function getInstallationToken(env) {
  const now = Math.floor(Date.now() / 1000);
  // Reuse with a 60s safety margin so we never hand out an about-to-expire token.
  if (cachedToken && cachedToken.exp - 60 > now) return cachedToken.token;

  const jwt = await appJwt(env);
  const resp = await fetch(
    `https://api.github.com/app/installations/${env.GH_INSTALLATION_ID}/access_tokens`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${jwt}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub requires a User-Agent on every API request.
        "User-Agent": "nellis-hunter-bot",
      },
    },
  );
  if (!resp.ok) {
    throw new Error(`installation token failed ${resp.status}: ${await resp.text()}`);
  }
  const body = await resp.json();
  cachedToken = { token: body.token, exp: Math.floor(Date.parse(body.expires_at) / 1000) };
  return body.token;
}

async function dispatchWorkflow(env, inputs) {
  const token = await getInstallationToken(env);
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub requires a User-Agent on every API request.
      "User-Agent": "nellis-hunter-bot",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs }),
  });
  if (!resp.ok) {
    // Surface the failure to the command instead of a silent "thinking…" forever.
    const detail = await resp.text();
    throw new Error(`GitHub dispatch failed ${resp.status}: ${detail}`);
  }
}

async function editInteraction(applicationId, token, content) {
  // PATCH @original to replace the deferred placeholder (used on errors).
  await fetch(
    `https://discord.com/api/v10/webhooks/${applicationId}/${token}/messages/@original`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    },
  );
}

function json(body) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("Nellis Hunter bot is alive.", { status: 200 });
    }

    const rawBody = await request.text();
    const valid = await verifySignature(request, rawBody, env.DISCORD_PUBLIC_KEY);
    if (!valid) return new Response("Bad request signature", { status: 401 });

    const interaction = JSON.parse(rawBody);

    if (interaction.type === InteractionType.PING) {
      return json({ type: InteractionResponseType.PONG });
    }

    if (interaction.type === InteractionType.APPLICATION_COMMAND) {
      const data = interaction.data || {};
      if (data.name === "hunt") {
        const inputs = {
          category: optionValue(data.options, "category"),
          location: optionValue(data.options, "location") || "Phoenix|Mesa",
          keywords: optionValue(data.options, "keywords") || "",
          closing_within_hours: String(
            optionValue(data.options, "closing_within_hours") || 48,
          ),
          app_id: interaction.application_id,
          interaction_token: interaction.token,
        };

        // Fire the workflow after we've replied; if it fails, tell the user.
        ctx.waitUntil(
          dispatchWorkflow(env, inputs).catch((err) =>
            editInteraction(
              interaction.application_id,
              interaction.token,
              `⚠️ Couldn't start the hunt: ${err.message}`,
            ),
          ),
        );

        return json({
          type: InteractionResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
          data: {
            content: `🔍 Hunting **${inputs.category}**… results in ~30-90s.`,
          },
        });
      }
    }

    return new Response("Unhandled interaction", { status: 400 });
  },
};

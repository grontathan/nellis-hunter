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
 * Required secrets (set via `wrangler secret put <NAME>`):
 *   DISCORD_PUBLIC_KEY  — app's Public Key (Discord dev portal → General Info)
 *   GH_TOKEN            — fine-grained PAT with Actions: read/write on the repo
 * Required vars (in wrangler.toml [vars]):
 *   GH_REPO             — e.g. "grontathan/nellis-hunter"
 *   GH_REF              — branch to run the workflow on, e.g. "main"
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

async function dispatchWorkflow(env, inputs) {
  const url = `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
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

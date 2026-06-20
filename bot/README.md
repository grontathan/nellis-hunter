# Nellis Hunter — Discord `/hunt` command (serverless, $0)

Lets anyone in your Discord server run an on-demand hunt:

```
/hunt category:Home Improvement location:Both closing_within_hours:48
```

No always-on machine. The flow:

```
Discord  ──▶  Cloudflare Worker  ──▶  GitHub Actions  ──▶  results back to the command
            (verify + defer + dispatch)   (the actual scrape)
```

The Worker is a thin relay (free tier). The scrape runs in GitHub Actions
(free for public repos). The only thing you provision once is credentials.

---

## One-time setup

### 1. Create the Discord application
1. https://discord.com/developers/applications → **New Application**.
2. **General Information** → copy the **Application ID** and **Public Key**.
3. **Bot** (left nav) → **Reset Token** → copy the **bot token** (used only to
   register the command).
4. **OAuth2 → URL Generator** → scopes `applications.commands` (and `bot` if you
   want it in the member list) → open the generated URL → add it to your server.

### 2. Register the `/hunt` command
From the repo root, with the values from step 1:

```bash
DISCORD_APP_ID=<app id> DISCORD_BOT_TOKEN=<bot token> \
  python bot/register_commands.py
```

Global commands can take up to ~1h to appear the *first* time; later
re-registers are instant. Edit `CATEGORY_CHOICES` in `register_commands.py` to
change the dropdown.

### 3. Create a GitHub token for the Worker
A **fine-grained PAT** scoped to this repo with **Actions: Read and write**
(github.com → Settings → Developer settings → Fine-grained tokens). This lets
the Worker trigger the `hunt-on-demand.yml` workflow.

### 4. Deploy the Cloudflare Worker
```bash
cd bot
npm install -g wrangler        # if you don't have it
wrangler login
# set GH_REPO / GH_REF in wrangler.toml if your fork differs
wrangler secret put DISCORD_PUBLIC_KEY   # paste the Public Key from step 1
wrangler secret put GH_TOKEN             # paste the PAT from step 3
wrangler deploy                          # prints your Worker URL
```

### 5. Point Discord at the Worker
Discord dev portal → your app → **General Information** →
**Interactions Endpoint URL** = the Worker URL from step 4 → **Save**.
Discord sends a signed PING; the Worker answers it. If it saves, you're wired.

---

## Try it
In any channel the bot can see:

```
/hunt category:Tools
```

You'll see **"🔍 Hunting Tools…"** immediately, then the ranked digest replaces
it ~30-90s later (the GitHub Actions run time).

## Notes & costs
- **$0**: Cloudflare Workers free tier (100k req/day) and GitHub Actions
  (free on public repos) both cover this comfortably.
- On-demand hunts run with `--no-db`, so they show every match every time and
  never interfere with the scheduled daily digest's dedup.
- The daily digest (`nellis-hunter.yml`) is independent and keeps running on its
  cron whether or not the bot is set up.
- Resale uses the fast heuristic for `/hunt` (no eBay scrape) so the command
  returns promptly; the daily sweep can still use eBay comps.

## Troubleshooting
- **"Interactions Endpoint URL" won't save** → `DISCORD_PUBLIC_KEY` secret is
  wrong or missing; re-run `wrangler secret put DISCORD_PUBLIC_KEY`.
- **Command stuck on "🔍 Hunting…"** → the GitHub dispatch failed. Check the
  Worker logs (`wrangler tail`) and that `GH_TOKEN` has Actions write on the repo.
- **`/hunt` doesn't appear** → wait for first-time global propagation, or
  confirm the OAuth2 invite included `applications.commands`.

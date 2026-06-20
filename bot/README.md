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
From the repo root, with the values from step 1.

bash / macOS / Linux:

```bash
DISCORD_APP_ID=<app id> DISCORD_BOT_TOKEN=<bot token> \
  python bot/register_commands.py
```

PowerShell (Windows) — the inline `VAR=val` prefix above is bash-only and
won't set anything in PowerShell, so set the vars first:

```powershell
$env:DISCORD_APP_ID = "<app id>"
$env:DISCORD_BOT_TOKEN = "<bot token>"
python bot/register_commands.py
```

Global commands can take up to ~1h to appear the *first* time; later
re-registers are instant. Edit `CATEGORY_CHOICES` in `register_commands.py` to
change the dropdown.

### 3. Create a GitHub App for the Worker
The Worker triggers `hunt-on-demand.yml` as a **GitHub App** — a machine identity
that isn't tied to a personal account and whose access tokens auto-rotate, so
nothing expires on you. (A personal access token would work but is personal and
expires by design; not appropriate for an autonomous tool.)

1. github.com → Settings → Developer settings → **GitHub Apps** → **New GitHub App**.
   Homepage URL can be anything; you can leave the webhook **unchecked**.
2. **Permissions** → Repository → **Actions: Read and write** (that's the only one
   needed). Create the App.
3. On the App's page, **Generate a private key** — this downloads a `.pem`
   (PKCS#1 format).
4. **Install App** (left nav) → install it on **this repo only**. Note the
   **Installation ID** from the resulting URL: `…/installations/<id>`.
5. Note the **App ID** from the App's **General** settings.
6. Convert the key to PKCS#8, which the Worker's WebCrypto requires:
   ```bash
   openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt \
     -in your-app.private-key.pem -out app.pkcs8.pem
   ```
   The result starts with `-----BEGIN PRIVATE KEY-----`.

### 4. Deploy the Cloudflare Worker
First set `GH_APP_ID` and `GH_INSTALLATION_ID` (and `GH_REPO` / `GH_REF` if your
fork differs) in `wrangler.toml`. Then:

```bash
cd bot
npm install -g wrangler        # if you don't have it
wrangler login
wrangler secret put DISCORD_PUBLIC_KEY        # paste the Public Key from step 1
wrangler secret put GH_APP_PRIVATE_KEY < app.pkcs8.pem   # the PKCS#8 key from step 3
wrangler deploy                               # prints your Worker URL
```

PowerShell (Windows) — pipe the key in, since the inline redirect differs:

```powershell
wrangler secret put DISCORD_PUBLIC_KEY
Get-Content app.pkcs8.pem -Raw | npx wrangler secret put GH_APP_PRIVATE_KEY
npx wrangler deploy
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
  Worker logs (`wrangler tail`). Common causes: `GH_APP_ID` /
  `GH_INSTALLATION_ID` wrong, the App isn't installed on the repo, or
  `GH_APP_PRIVATE_KEY` isn't the PKCS#8 (`BEGIN PRIVATE KEY`) form from step 3.6.
- **`/hunt` doesn't appear** → wait for first-time global propagation, or
  confirm the OAuth2 invite included `applications.commands`.

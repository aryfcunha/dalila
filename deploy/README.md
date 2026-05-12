# Deploying Dalila on Google Cloud (free tier)

This is the **always-free** path: a single `e2-micro` VM in one of the free
regions (`us-west1`, `us-central1`, `us-east1`). No charges, no card expiry —
just the limits Google publishes for the [Always Free tier](https://cloud.google.com/free/docs/free-cloud-features#compute).

The VM stays on when your laptop is off. That's the whole point of doing this.

## What you'll end up with

- An Ubuntu 24.04 LTS VM with 1 vCPU, 1 GB RAM, 30 GB disk, 2 GB swap
- Claude Code CLI authenticated against your Pro/Max plan
- Dalila installed, DB initialised, Telegram bot polling
- A systemd service that:
  - Starts the bot on every VM boot
  - Restarts it within 10 s if it crashes
  - Caps memory at 800 MB (so a runaway can't lock the VM)
  - Streams logs to `journalctl`

Total LLM cost on top of your existing Pro/Max plan: **zero**.

## Cost notes (read these before you start)

The free tier is real but bounded. You can break the limits and start
getting billed if you're not careful:

| Limit | What stays free | What costs money |
|---|---|---|
| Compute | 1 `e2-micro` in `us-west1`/`us-central1`/`us-east1` | Bigger sizes, other regions |
| Disk | 30 GB standard persistent disk in free region | Larger or SSD persistent disk |
| Egress | 1 GB/month to most regions | Egress to China/Australia, or above 1 GB |
| Public IP | 1 ephemeral IPv4 (released when VM stops) | A reserved static IP costs ~$3/mo while unused |

**Bandwidth math for Dalila**: GDELT pulls ~14 MB/day, RSS ~5 MB/day,
Telegram polling is negligible. Roughly **600 MB/month** — comfortably
inside the 1 GB egress allowance, but don't crank ingest_interval below
30 min without re-doing this calculation.

If you ever see a charge: stop the VM (don't delete it — you'll lose the DB)
and ping someone before resuming.

## Step-by-step

### 1. Create the VM (web console — easiest)

1. Go to https://console.cloud.google.com → create a new project (call it
   `dalila-bot` or similar). You'll be asked for a credit card for identity
   verification; **the free tier doesn't bill it as long as you stay within
   limits**.
2. Enable the Compute Engine API (one click when prompted).
3. **Compute Engine → VM instances → Create instance**. Fill in:
   - **Name**: `dalila`
   - **Region**: `us-central1` (Iowa)  ← must be a free-tier region
   - **Zone**: any `us-central1-*`
   - **Machine configuration**: General purpose, series **E2**, machine type **e2-micro**
   - **Boot disk**: click *Change* → **Ubuntu 24.04 LTS** (Minimal x86/64), 30 GB **Standard persistent disk** (NOT SSD — SSD isn't in the free tier)
   - **Firewall**: leave defaults (you don't need inbound ports; the bot only makes outbound connections)
   - **Advanced → Networking → Network interfaces → External IPv4 address**: leave on *Ephemeral*. Don't reserve a static IP unless you have a reason — reserving costs ~$3/mo when the VM is stopped.
4. **Create**. Wait ~30 s for the VM to come up.

### 2. SSH in

Easiest path: in the VM list, click the **SSH** button next to `dalila`.
GCP opens a browser SSH terminal. (No keys to manage.)

If you prefer your own terminal, install `gcloud` locally and run:
```bash
gcloud compute ssh dalila --zone=us-central1-a
```

### 3. Run the bootstrap script

In the SSH session:

```bash
curl -fsSL https://raw.githubusercontent.com/aryfcunha/dalila/main/deploy/install-on-vm.sh -o install-on-vm.sh
bash install-on-vm.sh
```

This takes ~3 min. At the end it prints the three manual steps below.

### 4. Authenticate Claude Code

```bash
claude login
```

The CLI prints a URL. **Open it in your laptop browser**, approve the
sign-in (use the same Anthropic account that has the Pro/Max plan),
copy the code it shows you, paste it back into the SSH terminal.

Verify:
```bash
claude --version
echo 'hello' | claude -p --model claude-haiku-4-5 --max-turns 1
```

You should see Claude respond. If it says "you've hit your limit" — the
auth worked, the daily quota is just exhausted. That's fine.

### 5. Configure the Telegram token (and ACLED creds if you have them)

```bash
nano ~/dalila/.env
```

Set `TELEGRAM_BOT_TOKEN=...` (the one you used locally). Save (`Ctrl-O`, `Enter`, `Ctrl-X`).

### 6. Initialise the DB and start the service

```bash
cd ~/dalila
.venv/bin/python -m dalila init
.venv/bin/python -m dalila check         # everything should be green

sudo cp deploy/dalila@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now "dalila@$USER"
```

Check it's alive:
```bash
systemctl status "dalila@$USER"          # Active: active (running)
journalctl -u "dalila@$USER" -f          # live tail; Ctrl-C to exit
```

In Telegram, message your bot `/start`. You should get a subscription
confirmation. If you don't, look at the journalctl output for the cause.

### 7. Migrating your local SQLite DB (optional)

If you want to keep the ~1300 items you've already ingested locally,
SCP the DB across before starting the service:

```bash
# from your Windows laptop (PowerShell):
gcloud compute scp `
    "C:\Users\pc14043\Documents\Dalila\dalila\dalila.db" `
    "dalila:~/dalila/dalila.db" `
    --zone=us-central1-a
```

If you skip this, the VM starts fresh — the bot will fill the DB on its
own within a day.

## Routine operations

```bash
# tail logs
journalctl -u "dalila@$USER" -f

# restart after editing config
sudo systemctl restart "dalila@$USER"

# pull new code from GitHub
cd ~/dalila && git pull
.venv/bin/pip install -e .
.venv/bin/python -m dalila init          # applies any new migrations
sudo systemctl restart "dalila@$USER"

# stop the bot temporarily
sudo systemctl stop "dalila@$USER"
```

## Failure modes worth knowing about

- **OOM kill**: 1 GB RAM is tight. The systemd unit caps the bot at 800 MB
  to keep some headroom for the OS. If you see "Killed" in the logs,
  it's the kernel — usually a Node.js spike from `claude`. Swap absorbs
  most of it; if it's persistent, drop `MemoryMax` and look at what the
  classifier batch size is doing.

- **Disk fills up**: 30 GB is plenty for the DB and logs for years.
  `journalctl --vacuum-size=200M` if you ever want to trim journald.

- **Bandwidth charges**: monitor in *Compute Engine → VM instances →
  click `dalila` → Monitoring → Network egress*. Set a budget alert at
  $1/mo to catch any surprise.

- **The VM gets shut down by GCP**: free tier VMs occasionally get
  preempted for maintenance. The systemd `enable` means it auto-starts
  again on the next boot. No action needed unless `journalctl` shows a
  problem after restart.

- **You delete the project**: everything goes including the DB. The
  weekly habit to acquire: `gcloud compute scp dalila:~/dalila/dalila.db ./backups/`.

## Tearing down

```bash
gcloud compute instances delete dalila --zone=us-central1-a
gcloud projects delete dalila-bot
```

Or from the web console. Either fully removes everything and stops any
clock running.

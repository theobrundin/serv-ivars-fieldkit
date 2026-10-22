# SCP_DEPLOY.md — Updating a Pi from your Mac (copy-paste method)

This is the simple way to push a code update to a Pi that's **already been set
up** and is running. You copy the new code from your Mac to the Pi with `scp`,
then run one script on the Pi that puts everything in place and restarts it.

You do **not** need GitHub, git, or any developer setup for this. Just Terminal.

> This method is for **updating an existing, working unit**. First-time setup of
> a brand-new Pi still follows `DEPLOY.md`.

---

## Before you start — three things you need

1. **The new code on your Mac.** Unzip the `serv-pi` folder somewhere easy to
   find, like your Desktop. You should end up with a folder that contains a
   `plate_reader` folder and a `scripts` folder inside it.
2. **The Pi's address.** Either its Tailscale IP (looks like `100.x.x.x`) or its
   local IP. If you normally SSH in as `ssh serv@<something>`, that `<something>`
   is the address.
3. **The Pi powered on and reachable.** Test it: open Terminal and run
   `ssh serv@<address>`. If you get a prompt on the Pi, you're good. Type `exit`
   to come back to your Mac.

Throughout this doc, replace `<PI>` with that address. Example: if you connect
with `ssh serv@100.92.14.7`, then `<PI>` is `100.92.14.7`.

---

## Step 1 — Open Terminal on your Mac and go to the code folder

If you unzipped it to your Desktop:

```bash
cd ~/Desktop/serv-pi
```

Confirm you're in the right place — this must list `plate_reader` and `scripts`:

```bash
ls
```

If you don't see both, you're in the wrong folder. Find where you unzipped it
and `cd` there instead.

---

## Step 2 — Copy the code to the Pi

This copies code to a staging area in the Pi's home directory. It does **not**
copy credentials or touch the running system yet.

```bash
scp -r ./plate_reader ./scripts serv@<PI>:~/serv-pi-staging/
```

- It may ask for the Pi's password (or just work, if you use SSH keys).
- It will print a list of files as they copy. That's normal.
- If it says `No such file or directory` for the destination, run this once to
  make the folder, then repeat the `scp` line above:
  ```bash
  ssh serv@<PI> "mkdir -p ~/serv-pi-staging"
  ```

---

## Step 3 — Log into the Pi

```bash
ssh serv@<PI>
```

You're now typing commands **on the Pi** (your prompt will change). Everything
in Step 4 happens here.

---

## Step 4 — Run the update script

One command. It checks and stages the package, atomically swaps it into place,
keeps the unit's existing private config, restarts the service, and waits for
real camera plus inference health. If health does not arrive within two
minutes, it automatically restores the previous package.

```bash
sudo bash ~/serv-pi-staging/scripts/update_from_staging.sh
```

Watch the output. Every line should start with `[ok]`. At the end you want to
see:

```
[ok]   plate-reader reached real camera+inference health
[ok]   auto-updater paused with .manual_override until --resume
==== Update complete ====
```

If instead you see a red `[stop]` line, the script either stopped before the
swap or restored the previous healthy package. Read the message, fix what it
says (usually: re-do the scp in Step 2), and run it again.

This emergency/manual method pauses the Git updater so a timer cannot silently
erase the hotfix. After the same change is committed and pushed to the unit's
tracked channel, resume it with:

```bash
sudo bash /home/serv/serv-pi/scripts/update.sh --resume
```

---

## Step 5 — Watch it run (optional but recommended)

```bash
sudo journalctl -u plate-reader -f
```

You'll see live activity. With this build you'll also see occasional
`DRIVE_OFF` lines when a car passes through without being identified — that's the
new feature working. Press `Ctrl+C` to stop watching (this does **not** stop the
service, just the log view).

Type `exit` to leave the Pi and return to your Mac.

---

## If something goes wrong: rolling back

Startup failures roll back automatically. The timestamped previous package is
also retained for a deliberate manual rollback if a later behavioral issue is
found after the initial health gate.

When in doubt, stop and ping Theo. Don't improvise on a live pilot unit.

---

## Quick reference (once you've done it once)

```bash
# On your Mac, in the serv-pi folder:
scp -r ./plate_reader ./scripts serv@<PI>:~/serv-pi-staging/
ssh serv@<PI>
# On the Pi:
sudo bash ~/serv-pi-staging/scripts/update_from_staging.sh
sudo journalctl -u plate-reader -f    # watch; Ctrl+C to stop watching
exit
```

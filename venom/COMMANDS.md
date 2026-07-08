# Venom — command cheat sheet

Copy-paste commands for the whole loop: **code → git → deploy to Pi → verify →
debug**. Companion to `WORKFLOW.md` (which explains *what* to edit; this is
*what to type*).

Placeholders: `venom.local` = the Pi, user `hrishikeshjhaa`, branch `v2/rebuild`,
repo on the Pi at `/opt/venom/app`, venv at `/opt/venom/venv`.
SSH is **key-based** (no password). **sudo** on the Pi asks for your login password.

---

## 0. Where things are

| Thing | Path |
|-------|------|
| Local repo | `C:/Projects/Personal/FLINT` |
| Venom project | `C:/Projects/Personal/FLINT/venom` |
| Repo on Pi | `/opt/venom/app` (root-owned) |
| Installed package on Pi | `/opt/venom/venv/lib/python3.13/site-packages/venom/` |
| Live config on Pi | `/etc/venom/venom.toml` |
| Runtime status | `/run/venom/status.json` |
| Memory/notes/reminders | `/var/lib/venom/` |

---

## 1. Git workflow (local — Windows / Git Bash or PowerShell)

```bash
cd C:/Projects/Personal/FLINT

git status                       # what changed
git branch                       # confirm you're on v2/rebuild
git diff                         # review unstaged changes
git diff --staged                # review staged changes

git add -A                       # stage everything
git add path/to/file             # …or just one file

git commit -m "Short message"    # commit
git push origin v2/rebuild       # push to GitHub

git pull origin v2/rebuild       # get latest (if editing from 2 machines)
git log --oneline -10            # recent history
```

Undo / recover:
```bash
git restore path/to/file         # discard unstaged edits to a file
git restore --staged file        # unstage (keep edits)
git reset --soft HEAD~1          # undo last commit, KEEP changes staged
git revert <hash>                # make a new commit that undoes <hash> (safe)
git stash / git stash pop        # shelve changes / bring them back
```

> Commit messages in this repo end with a trailing line:
> `Co-Authored-By: Claude ...` — optional for your own commits.

---

## 2. SSH into the Pi

```bash
ssh hrishikeshjhaa@venom.local           # open a shell on the Pi

# if venom.local won't resolve, find the IP first:
ping -4 venom.local                      # (from Windows) shows the IPv4
ssh hrishikeshjhaa@10.x.x.x              # …then use the IP

# run ONE command without opening a shell:
ssh hrishikeshjhaa@venom.local "systemctl is-active venom.service"

# run a sudo command non-interactively (it will prompt for the password):
ssh hrishikeshjhaa@venom.local "sudo systemctl restart venom.service"
```

Root block (several sudo commands at once) — from inside an SSH shell:
```bash
sudo bash -c '
  echo "commands here run as root"
  systemctl restart venom.service
'
```

---

## 3. Deploy your pushed code to the Pi

### A. Normal case — code-only change (fast, reliable)
Run from inside `ssh hrishikeshjhaa@venom.local`:
```bash
sudo bash -c '
  git -C /opt/venom/app fetch --quiet origin v2/rebuild
  git -C /opt/venom/app reset --hard origin/v2/rebuild
  /opt/venom/venv/bin/pip install --quiet --force-reinstall --no-deps /opt/venom/app/venom
  systemctl restart venom.service
'
```

### B. You added a new dependency (e.g. `chess`)
`--no-deps` won't install it, so install it first:
```bash
sudo bash -c '
  git -C /opt/venom/app fetch --quiet origin v2/rebuild
  git -C /opt/venom/app reset --hard origin/v2/rebuild
  /opt/venom/venv/bin/pip install --quiet "chess>=1.11"      # <-- the new dep
  /opt/venom/venv/bin/pip install --quiet --force-reinstall --no-deps /opt/venom/app/venom
  systemctl restart venom.service
'
```
(Also add it to `pyproject.toml` `dependencies` so full provisions get it.)

### C. One-liner from your laptop (no interactive shell)
```bash
ssh hrishikeshjhaa@venom.local "sudo bash -c 'git -C /opt/venom/app fetch -q origin v2/rebuild && git -C /opt/venom/app reset --hard origin/v2/rebuild && /opt/venom/venv/bin/pip install -q --force-reinstall --no-deps /opt/venom/app/venom && systemctl restart venom.service'"
```

### D. The lazy way
Press **update** in the web dashboard, or reboot — `venom-provision.service`
does git-fetch + reinstall + restart on boot. (Slower; SSH is flaky for ~1–2
min right after a reboot.)

---

## 4. Verify the deploy actually landed

```bash
# HEAD on the Pi matches what you pushed?
ssh hrishikeshjhaa@venom.local "git -C /opt/venom/app rev-parse --short HEAD"

# the INSTALLED copy (not just the repo) has your change — grep a marker:
ssh hrishikeshjhaa@venom.local "grep -c 'MY MARKER' /opt/venom/venv/lib/python3.13/site-packages/venom/live.py"

# service healthy?
ssh hrishikeshjhaa@venom.local "systemctl is-active venom.service"
```

> Always check the **installed copy** under `site-packages/venom/`, not
> `/opt/venom/app` — provisioning can skip the reinstall and leave an old
> process running.

---

## 5. Service control & logs

```bash
sudo systemctl restart venom.service      # restart (most common)
sudo systemctl stop venom.service         # stop
sudo systemctl start venom.service        # start
systemctl status venom.service            # status + last log lines

journalctl -u venom -n 80                 # last 80 log lines
journalctl -u venom -f                    # live tail (Ctrl-C to quit)
journalctl -u venom | grep "tool: "       # every tool the model called
journalctl -u venom -n 300 | grep -i chess

cat /run/venom/status.json                # internet / headset / brain / version
```

`tool: <name> {args}` in the log is the #1 debugging line — it shows exactly
what the model invoked and with what arguments.

---

## 6. Edit config ON THE DEVICE (repo edits don't reach it)

`/etc/venom/venom.toml` is preserved by provisioning, so change it live:
```bash
ssh hrishikeshjhaa@venom.local
sudo nano /etc/venom/venom.toml           # edit
sudo systemctl restart venom.service      # apply
```
Quick one-off value without nano:
```bash
sudo sed -i 's/^user_name = .*/user_name = "Boss"/' /etc/venom/venom.toml
sudo systemctl restart venom.service
```

---

## 7. Local dev & test (before you push)

```bash
cd C:/Projects/Personal/FLINT/venom

python -m pytest tests/ -q                        # whole suite
python -m pytest tests/test_chess_game.py -q      # one file
python -m pytest tests/test_voice_stack.py -q -k music   # one topic

python -c "import ast; ast.parse(open('src/venom/chess_game.py').read()); print('ok')"  # syntax check

pip install "chess>=1.11"                          # install a new dep locally to test
```

---

## 8. Screen server (runs on the LAPTOP, not the Pi)

For "look at my screen". Auto-starts via Task Scheduler task `VenomScreenServer`.
```powershell
Get-ScheduledTask VenomScreenServer                # is it registered?
Start-ScheduledTask VenomScreenServer              # start it
Get-NetTCPConnection -LocalPort 8766               # is it listening?
```

---

## 9. Dashboard

```
http://<pi-ip>:8787        # PIN = web_token in venom.toml
```
Get the Pi's current IP (it drifts on hotspot DHCP):
```bash
ssh hrishikeshjhaa@venom.local "hostname -I"
```

---

## 10. The whole loop in one glance

```bash
# 1. edit code locally in venom/src/venom/...
# 2. test
python -m pytest tests/ -q
# 3. commit + push
git add -A && git commit -m "msg" && git push origin v2/rebuild
# 4. deploy
ssh hrishikeshjhaa@venom.local "sudo bash -c 'git -C /opt/venom/app fetch -q origin v2/rebuild && git -C /opt/venom/app reset --hard origin/v2/rebuild && /opt/venom/venv/bin/pip install -q --force-reinstall --no-deps /opt/venom/app/venom && systemctl restart venom.service'"
# 5. verify
ssh hrishikeshjhaa@venom.local "systemctl is-active venom.service && git -C /opt/venom/app rev-parse --short HEAD"
# 6. talk to her / watch logs
ssh hrishikeshjhaa@venom.local "journalctl -u venom -f"
```

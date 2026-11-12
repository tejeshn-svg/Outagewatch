# Putting OutageWatch online, for free, forever

When this is done, OutageWatch runs on GitHub's computers every 10 minutes.
Your PC can be off, asleep, or in a drawer. You get a public web address and
Telegram alerts either way.

Cost: nothing. No server, no credit card, nothing to keep secure.

---

## Before you start: the one thing that matters

**Never upload `config.json`.** It holds your Telegram bot token, and the
repository will be public — anyone could read it and post as your bot.

The same goes for these, which are either private or just clutter:

```
config.json          <- YOUR TOKEN. never upload this one.
outagewatch.db       alerts.log       vendors.json.backup
diagnose.txt         sample.txt       microsoft.txt      feed.txt
__pycache__          docs/            state/
```

`.gitignore` protects you if you ever use git properly, but **it does not
protect a drag-and-drop upload**. When uploading by hand, you are the filter.

Your token goes in GitHub's Secrets instead — step 4 — where it stays hidden.

---

## 1. Make a GitHub account

[github.com](https://github.com) → Sign up. Free. Two minutes.

## 2. Make a repository

Click **+** (top right) → **New repository**.

- **Name:** `outagewatch`
- **Public** — this matters: scheduled jobs are only free on public
  repositories. Nothing private lives here, as long as you follow the rule above.
- Leave everything else alone, click **Create repository**.

## 3. Upload the files

On the new empty repository page, click **uploading an existing file**.

From your `outagewatch` folder, drag in **only** these:

```
collector.py        app.py            build_static.py
vendors.json        requirements.txt  README.md
discover.py         fixvendors.py     selftest.py
setup_alerts.py     test_alert.py     dump_feed.py
.gitignore          HOSTING.md
static/             templates/        .github/
```

The `.github` folder is the important one — it contains the schedule. If
your file manager hides folders starting with a dot, turn on **View → Hidden
items** in Windows Explorer.

Click **Commit changes**.

## 4. Add your Telegram token as a secret

In your repository: **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**.

Add two, taking the values from your `config.json`:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the long token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat id |

Secrets are write-only — once saved, not even you can read them back, and they
never appear in logs. This is the right place for a token.

## 5. Turn the schedule on

Go to the **Actions** tab. GitHub asks you to confirm workflows — click the
green button to enable them.

Pick **OutageWatch** on the left, then **Run workflow** → **Run workflow** to
start one immediately rather than waiting.

Give it a minute, then click into the run to watch it. You want to see
`checked 33 vendors`.

## 6. Switch the website on

**Settings** → **Pages**.

- **Source:** Deploy from a branch
- **Branch:** `main`, folder **`/docs`** → **Save**

Wait a minute or two. Your status page is then at:

```
https://<your-username>.github.io/outagewatch/
```

That address is public. Send it to anyone.

---

## What happens from now on

Every 10 minutes GitHub starts a fresh machine, polls all 33 vendors, sends any
Telegram alerts, rebuilds the web page, and saves the result back to the
repository. Then the machine is thrown away.

The `state/` folder is how one run remembers the last — it holds the current
status, the full event history and any active correlations as plain JSON.
That's also why the history page keeps working even though nothing is
permanently running.

## Things worth knowing

**It can run late.** GitHub's minimum is 5 minutes and scheduled jobs often
start a few minutes behind under load. For a status page that's fine.

**It switches itself off if you ignore it.** GitHub disables scheduled
workflows in repositories with no activity for 60 days. Any commit wakes it
up — or just click Run workflow occasionally.

**Your data is public.** The repository, the status history, the web page.
There is nothing sensitive in it, provided `config.json` stayed on your PC.

**Your PC is now optional.** `run.bat` still works if you want to watch it
locally, but nothing depends on it any more.

---

## If something goes wrong

Open the **Actions** tab and click the failed run — the error is in the logs.

| What you see | What it means |
|---|---|
| `unknown kind '...'` | `collector.py` and `vendors.json` are out of step; re-upload both |
| No Telegram messages | Check the two secret names are spelled exactly as above |
| Page is 404 | Pages needs branch `main` and folder `/docs`; give it a few minutes |
| Workflow never runs | Actions tab → enable workflows; check the repository is public |
| `Permission denied` on push | Settings → Actions → General → Workflow permissions → Read and write |

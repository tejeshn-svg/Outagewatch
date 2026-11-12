# OutageWatch

**By [tejeshn-svg](https://github.com/tejeshn-svg)** · [Live status page](https://tejeshn-svg.github.io/Outagewatch/) · MIT licensed

A self-hosted outage aggregator, modelled on [status.networkthinking.com](https://status.networkthinking.com).
It polls each vendor's **own official status feed**, translates every vendor's
private vocabulary into one set of five levels, and shows them on a single page
with notifications when anything changes state.

```
collector.py  ──writes──▶  outagewatch.db  ──reads──▶  app.py  ──▶  browser
   (poller)                   (SQLite)                 (Flask)
```

Those two halves stay separate on purpose: a slow vendor can never make your
page slow, and you can restart either one without touching the other.

---

## Setup on Windows

Install Python 3.11+ from python.org — **tick "Add Python to PATH"** in the
installer. Then open Command Prompt in this folder:

```bat
pip install -r requirements.txt
python collector.py --once          REM one pass, fills the database
python app.py                       REM then open http://127.0.0.1:5000
```

Or just double-click **`run.bat`**, which starts the collector in a loop and the
web app together.

To check everything works without touching the network:

```bat
python selftest.py
```

That builds fake-but-real-shaped feeds, runs all five adapters, and verifies the
transition detection. It should end with `SELF-TEST PASSED`.

---

## The five levels

Every vendor on earth gets mapped into one of these. This normalisation is the
actual product — fetching JSON is the easy part.

| Level | Shown as | Meaning |
|---|---|---|
| `operational` | ● Operational | everything fine |
| `maintenance` | ■ Maintenance | planned work in progress |
| `degraded` | ▲ Degraded Performance | slow, erratic, or one component unhappy |
| `partial_outage` | ◆ Partial Outage | some components or regions down |
| `major_outage` | ✖ Service Outage | the service is down |
| `unknown` | ? Unknown | **we couldn't reach the feed** — not the same as "up" |

`unknown` matters. A lot of homemade status boards silently show green when
their own collector is broken. This one tells you it doesn't know.

---

## The trick that makes this possible

A large share of well-known services run on **Atlassian Statuspage**, and every
one of those pages serves the same public JSON with no API key and no scraping:

```
https://<their status domain>/api/v2/summary.json
```

One adapter therefore covers most of your vendor list. Learn that one shape and
adding a vendor is a line of JSON, not code.

Statuspage is not the only platform, though. Instatus and Status.io take most of
the remainder, and each publishes its own public JSON in its own vocabulary. When
a vendor shows as **Unknown**, run `fixvendors.py`: it tries the addresses
companies actually use, works out which platform answers, and writes both the
address and the correct `kind` back into `vendors.json`.

To find out whether a vendor qualifies, use the discovery tool:

```bat
python discover.py status.somevendor.com
python discover.py vendor.com anothervendor.com
```

It tries the obvious hostnames (`status.x.com`, `www.xstatus.com`, …), reports
whether the Statuspage API answered, and prints a ready-to-paste `vendors.json`
line. This is how you get from 34 vendors to a few hundred without writing
anything new.

## Adding vendors

Edit `vendors.json`. No Python changes needed.

```json
{ "slug": "acme", "name": "Acme", "kind": "statuspage",
  "url": "https://status.acme.com", "tags": ["SaaS"] }
```

`kind` is one of:

| kind | use for |
|---|---|
| `statuspage` | anything on Atlassian Statuspage (most vendors) |
| `instatus` | anything on Instatus — serves `/summary.json` |
| `fastly` | Fastly's homemade JSON at `www.fastlystatus.com/summary.json` |
| `status_io` | anything on Status.io (GitLab, for one). The `url` is the API endpoint itself: `https://api.status.io/1.0/status/<page id>` |
| `gcp` | Google Cloud's `incidents.json` |
| `azure_rss` | the public Azure status RSS feed |
| `aws` | the AWS public current-events feed |
| `probe` | **no feed at all** — just ask the front door if it answers |

`probe` is the crude one, and also the honest one: it catches outages before the
vendor has admitted to them.

### Vendors deliberately left out

**PagerDuty, Zendesk and Algolia are not in the list.** All three run their
status pages as JavaScript applications: every address returns the same empty
HTML shell and the real data is fetched later by script. There is no public feed
to read.

Leaving them in would mean three permanent "Unknown" tiles, and a dashboard that
quietly shows question marks is worse than one that covers fewer services
honestly. If you want them back, the leads found in their JavaScript bundles
were `status.pagerduty.com/api/data` and `status.algolia.com/incidents`. Zendesk's
bundle contained no status endpoint at all.

### The three Microsoft feeds

Microsoft doesn't do Statuspage-style JSON, but it does publish RSS in three
separate places, and they cover different products:

| Feed | Covers |
|---|---|
| `azure.status.microsoft/en-us/status/feed/` | Azure, the cloud platform |
| `status.cloud.microsoft/api/feed/mac` | Microsoft 365 admin centre |
| `status.cloud.microsoft/api/feed/ppac` | Power Platform admin centre |

The last two are undocumented. They were found by loading the status page and
reading its own JavaScript to see which endpoints it fetches — the same trick
that works on most JavaScript status pages. Neither needs a login.

Note the limit: these are **admin-centre** feeds. They are not the full
per-service Microsoft 365 health data, which really does live behind the Graph
API and really does need a tenant app registration. For complete Outlook /
Teams / Exchange detail you would still need that. What these give you is
Microsoft's public acknowledgement — which is better than nothing and, as the
correlation feature below shows, often later than everyone else's.

---

## Correlated outages

A company is slow to admit its own outage. It has no reason to hide somebody
else's. When several unrelated vendors all blame the same third party at once,
that is strong evidence — and it often arrives before the third party says a
word, sometimes for a service you cannot monitor directly at all.

The collector watches every vendor's incident text for mentions of major
providers. When **three or more independent vendors** name the same one, a
banner appears at the top of the dashboard and an alert is sent.

Two rules keep it honest:

- **No self-corroboration.** Azure mentioning Microsoft doesn't count; that's
  Microsoft talking about Microsoft.
- **Affiliated vendors count once.** SendGrid is owned by Twilio and
  republishes its incidents, so they collapse into one voice (shown as
  "SendGrid/Twilio"). Counting them separately would inflate the evidence with
  one company's opinion twice.

This is crowdsourcing without a crowd. The reporters are other companies'
engineering teams, already writing honest incident notes for their own
customers. The banner says "may be having problems" because this is inference,
not an announcement — and it should keep saying that.

Tune the entity list, the affiliations and the threshold near the top of
`collector.py`.

## Notifications

Alerts fire **only on a state change**, which is the whole point — nobody wants
a message every two minutes saying GitHub is still fine.

Every change is written to `alerts.log` and printed to the console. For push
notifications to your phone, create `config.json`:

```json
{
  "telegram_bot_token": "123456:ABC-your-token",
  "telegram_chat_id": "987654321",
  "alert_on_unknown": false
}
```

To get those two values: message `@BotFather` on Telegram, send `/newbot`, and
it gives you the token. Then message your new bot once and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat id.

Set `alert_on_unknown` to `true` if you also want to be told when a status feed
itself becomes unreachable. Noisy, but informative.

---

## Your own API

```
GET /api/status.json
GET /api/status.json?issues=1
GET /api/status.json?tag=Cloud
```

This endpoint is worth noticing. Companies sell exactly this — normalised,
aggregated outage data as an API — and it costs you nothing to expose because
you already did the normalising.

---

## Where this goes next

Roughly in order of value:

1. **"Is it down, or is it just me?"** — join this against your ISP
   Accountability Tracker. When Teams won't load, answer the actual question:
   *your* link is clean and Microsoft is degraded, or Microsoft is fine and your
   packet loss spiked four minutes ago. Nobody does this well, and you already
   have both halves.
2. **Run it somewhere that doesn't sleep.** A PC under your desk can't be a
   status page. A $5/month VPS, or a free tier, and the collector as a service.
3. **Grow the vendor list** with `discover.py` until it's useful to someone
   other than you.
4. **A daily narrative summary** like the reference site's — the data is already
   in the `events` table; it's a paragraph generated from the day's transitions.
5. **Email alerts** alongside Telegram, and per-vendor subscriptions.
6. **Fold it into NetGuardian** as the outage module. This is the same shape as
   the rest of that product: collect, normalise, explain.

## Files

| File | What it is |
|---|---|
| `collector.py` | the poller, the adapters, the normaliser, the alerting |
| `app.py` | Flask web app: dashboard, history, JSON API |
| `vendors.json` | the vendor list — edit this, not the code |
| `discover.py` | tests whether a domain speaks Statuspage |
| `selftest.py` | offline verification of every adapter |
| `templates/`, `static/` | the page itself |
| `outagewatch.db` | created on first run; delete it to start over |

# Sky Events

A Home Assistant integration that answers one question honestly: **is anything
worth looking up at tonight?**

It publishes a qualitative aurora outlook and a single "most noteworthy sky
event" — solar or lunar eclipse, notable full-moon rise, meteor shower, or
aurora — computed for *your* Home Assistant location, and fires a bus event
once per notable stage so you can notify however you like.

## What it does

- **Aurora outlook** — NOAA SWPC's three-day planetary Kp forecast, paired
  with your hourly cloud forecast and darkness at your coordinates. States are
  deliberately qualitative: `Unlikely`, `Possible`, `Promising tonight`,
  `Look outside now`, `Unavailable`. It does **not** invent a visibility
  percentage.
- **Eclipses** — calculated locally for your site, not copied from regional
  values. Trivial events are suppressed (solar below 0.10 obscuration,
  penumbral lunar).
- **Full-moon rises** — only when the Moon is ≥95% illuminated, rises within
  an hour of sunset, and the sky is usably clear. Close-perigee rises are
  labelled supermoons.
- **Meteor showers** — from a local, source-backed subset of the annually
  issued IMO Meteor Shower Calendar. The published ZHR is shown as an *ideal
  zenithal reference rate*, never as a prediction of what you personally will
  see.
- **Urgency-first ranking** so a remote eclipse cannot hide a live aurora, and
  a routine shower cannot hide an eclipse.

Everything degrades to `Unavailable` with a specific explanation rather than
guessing. Stale inputs are reported as attributes, not hidden.

## It sends no notifications — on purpose

This integration publishes state and fires `sky_events_stage` on the event
bus. It has no idea who you are, which devices you own, or whether you want to
be woken at 03:00. That is your automation's decision, which also means this
integration carries no recipient list, no quiet-hours policy and no transport.

```yaml
automation:
  - alias: Sky event alerts
    triggers:
      - trigger: event
        event_type: sky_events_stage
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "{{ trigger.event.data.title }}"
          message: "{{ trigger.event.data.message }}"
```

Each stage fires **once, ever** — persisted across restarts — so you get one
"solar glasses" reminder a month before an eclipse, not one every five
minutes. Event data includes `kind` (`aurora`, `sky_event`, `maintenance`),
`event_type`, `stage`, `title`, `message`, and where relevant `cloud_cover`,
`peak` and `details_url`.

## Entities

| Entity | Meaning |
|---|---|
| `sensor.sky_events_aurora_outlook` | The qualitative aurora state |
| `sensor.sky_events_noteworthy_sky_event` | Title of the top-ranked event, with full detail in attributes |

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository, category
   **Integration** → Download.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Sky Events.**

It observes **Home Assistant's own configured location**, so there is nothing
to type in: set your location under *Settings → System → General* and it is
correct at every site. You are asked only for a weather entity (hourly cloud
cover) and, optionally, live aurora sensors.

### Thresholds

All adjustable under the integration's **Configure** button, no restart
needed.

**Kp thresholds depend on your geomagnetic latitude.** The defaults (`5` for
Possible, `6` for Promising) suit roughly 50° N. Further from the poles, raise
them; nearer, lower them. This is why they are options rather than constants.

## Meteor data maintenance

The meteor dataset is hand-maintained and **deliberately expires** rather than
extrapolating into a year nobody has published yet. The integration warns you
60 days before expiry and again once it lapses, via a `maintenance` stage
event, and exposes `meteor_data_expires` / `meteor_data_days_remaining` as
attributes.

When it expires, replace `data/meteor_showers_*.json` from the current IMO
calendar — peak intervals, rates, radiant coordinates, source URLs — and
extend `supported_years`. Do not shift the previous year's times forward: real
peaks move by roughly +6h per common year and −18h after a leap year, so
arithmetic is not a substitute for the published calendar.

> **Known defect in the shipped data:** the 2027 rows were extrapolated, not
> sourced. Six of seven showers duplicate their 2026 peak times exactly, which
> cannot be right. `tests/test_providers.py` documents this as an expected
> failure. 2026 is unaffected. Replace the file before relying on 2027.

## Credits

Local astronomy uses [Astronomy Engine](https://github.com/cosinekitty/astronomy)
(MIT, by Don Cross). Eclipse reference material is NASA's; meteor data derives
from the International Meteor Organization's calendar, cross-checked against
the American Meteor Society's major-shower list. Aurora forecasting uses
NOAA SWPC's public product.

Forecasts and cloud cover are inherently uncertain. This is decision support,
not a guarantee of anything.

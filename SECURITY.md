# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) (the
**Security** tab → **Report a vulnerability**), not in a public issue.

Only the latest version is supported; fixes ship in a new release.

## Scope

UniScale stores personal health data: lab results, ECGs, blood pressure,
body composition, sleep and raw DNA. Of particular interest are anything that
lets a client read or write records without the configured `api_token`,
anything that sends that data somewhere other than the configured `[ai]`
endpoint or sync hub, script injection into the web dashboard, and anything
in the Android app that exposes the API token or synced data to other apps.

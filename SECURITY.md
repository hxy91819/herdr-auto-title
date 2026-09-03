# Security Policy

## Reporting a vulnerability

Please open a private security advisory via GitHub ("Security" → "Report a vulnerability") instead of a public issue.

## What this plugin does with your data

- Pane content is read from herdr (recent terminal output) and sent **only** to the local title-provider CLI you configure (by default the `codebuddy` CLI running on your machine). Nothing is uploaded anywhere else.
- The provider subprocess runs in a neutral working directory with `HERDR_*` environment variables stripped, so it never sees herdr's session context or credentials.
- No prompt or response content is persisted. Only state bookkeeping (`state.json`: pane ids, titles, counters) and a decision-level diagnostic log (`auto-title.log`) are written, both `0600` inside herdr's plugin state directory.

## What not to commit here

- `.env*`, tokens, private keys, certificates
- provider logs, pane transcripts, session recordings, or anything containing pane content
- machine-specific paths or hostnames

## Supported versions

Only the latest `main` branch is supported.
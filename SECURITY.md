# Security Policy

## Sensitive Data Notice

Receipt data processed by this tool is **personal financial information**. Please handle it carefully:

- **Never commit `config/settings.toml`** — it contains local filesystem paths specific to your machine.
- **Never commit `data/`** — the SQLite database contains parsed receipt data.
- **Never commit `reports/`** — generated reports contain spending summaries.
- **Never commit actual receipt PDFs** or any personal financial data into version control.

Both `config/settings.toml`, `data/`, and `reports/` are gitignored by default.

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it privately rather than opening a public issue.

Contact the maintainer directly via GitHub private message or by email as listed on their GitHub profile. Include:

- A description of the vulnerability
- Steps to reproduce
- Potential impact

You will receive a response within 5 business days. Please do not disclose the issue publicly until a fix is available.

# Security and Evidence Handling

CryptoHound processes financial and tax evidence. Do not commit runtime databases,
imported statements, transaction exports, tax forms, wallet evidence, backups, or API keys.

Keep secrets in environment variables and keep source evidence outside version control.
If sensitive material is committed accidentally, treat it as exposed: remove it from Git
history as appropriate and rotate any affected credentials.

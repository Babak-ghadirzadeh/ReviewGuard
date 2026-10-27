# Security

This alpha is designed for one local editor/researcher. The server binds only to 127.0.0.1 and has no multi-user authentication. Do not expose its port to the public internet or put confidential manuscripts into an unreviewed shared deployment.

Public-source HTTP requests reject private/local destinations and credential-bearing URLs. Redirect checks strip cross-host authorization. Responses, archive extraction and form sizes are bounded. Article and model text is untrusted data; it cannot authorize commands, author contact or publication. AI proposals do not bypass the evidence rules.

Report security defects privately using GitHub's private vulnerability reporting when enabled. Otherwise contact the repository owner through an established private channel; do not publish exploit details or secrets in issues. No private reporting endpoint is claimed to be active before repository setup.

Source credentials should be supplied through the local environment or GitHub's normal authentication tools. Never put tokens in issues, chat, source files or screenshots. Local data contains evidence and editor decisions; protect it using normal operating-system access controls and backups.

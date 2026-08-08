# Security notes

The root README is authoritative. Production hardening must include host firewall, SSH key-only access, fail2ban, private PostgreSQL/Redis networks, encrypted secrets, JWT key rotation, TOTP, audit logs, container sandboxing, and explicit reauthentication for high-risk owner actions.

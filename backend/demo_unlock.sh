#!/bin/sh
# DEMO ONLY: unlock the synthetic demo staff accounts and clear the
# in-memory rate-limit counters. Touches no patient data, consent,
# documents, keys or audit events.
cd "$(dirname "$0")"
sqlite3 identity.db "update staff_users set locked_until=NULL, failed_logins=0 where username in ('dr.example','dr.other','auditor','admin');"
curl -s -X POST http://127.0.0.1:8000/api/v1/demo/rate-limit/reset >/dev/null
sqlite3 -column identity.db "select username, failed_logins, coalesce(locked_until,'unlocked') as status from staff_users;"
echo
venv/bin/python -m app.security.demo_totp

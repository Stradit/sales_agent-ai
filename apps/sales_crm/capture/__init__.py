"""Activity capture (apps/sales_crm/README.md §4, M4): Microsoft 365 email + calendar → CRM activities.

crypto.py     token encryption (Fernet)
microsoft.py  OAuth (auth-code + PKCE) and Microsoft Graph delta reads, normalised records
engine.py     matching rules, idempotent upsert into activities, per-connection sync, worker
api.py        /api/crm/capture/* endpoints
selftest.py   end-to-end check against a fake Graph (no credentials needed)
"""

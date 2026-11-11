"""Account intelligence pipeline — single entry point.

Manages the whole process: scrape the configured accounts, store results
without duplicates, generate the sales/social digests, and serve the frontend.

    python main.py scrape bny --limit 20
    python main.py digest bny
    python main.py run --all              # scrape, then digest
    python main.py status
    python main.py serve

Every subcommand accepts --help.
"""
import argparse
import asyncio
import json
import os
import sys

from engine import scrape_and_store
from paths import DIGEST_DIR, OUTPUT_DIR, PROJECT_ROOT, store_path as _resolved_store_path
from targets import COMPANY_TARGETS, resolve as resolve_company
from people_targets import PEOPLE_TARGETS, resolve as resolve_person

DEFAULT_PORT = 8001

# Which target field means a channel is configured — used by /api/accounts
# and /api/people to report "channels" without re-deriving engine.py's
# want()/scrape_all() wiring.
CHANNEL_FIELDS = {
    "linkedin": "linkedin_url",
    "twitter": "twitter_handle",
    "reddit": "reddit_query",
    "sec": "sec_cik",
    "news": "news_query",
    "blog": "blog_url",
    "newsroom": "newsroom_url",
    "patents": "patents_query",
    "rss": "rss_url",
    "youtube": "youtube_channel_id",
    "sec_mentions": "sec_mentions_query",
    "regulatory": "regulatory_query",
    "linkedin_jobs": "linkedin_jobs_query",
}


def _configured_channels(target: dict) -> list:
    return [ch for ch, field in CHANNEL_FIELDS.items() if target.get(field)]


def _table(args) -> dict:
    return PEOPLE_TARGETS if getattr(args, "person", False) else COMPANY_TARGETS


def _resolve(args, key: str) -> dict:
    return resolve_person(key) if getattr(args, "person", False) else resolve_company(key)


def _companies(args) -> list:
    """Which targets (companies, or people with --person) a subcommand should act on."""
    table = _table(args)
    if getattr(args, "all", False):
        return sorted(table)
    if args.company:
        return [args.company]
    return sorted(table)


def _store_path(args, company: str) -> str:
    return _resolved_store_path(_resolve(args, company)["key"])


def _split(value: str) -> list:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


# ── scrape ───────────────────────────────────────────────────────

async def cmd_scrape(args) -> int:
    failures = 0
    kind = "person" if args.person else "company"
    for company in _companies(args):
        target = _resolve(args, company)
        _banner(f"SCRAPE · {target['display_name']}")
        try:
            await scrape_and_store(
                company,
                limit=args.limit,
                only=_split(args.only),
                include_newsroom=not args.no_newsroom,
                out_path=_store_path(args, company),
                reset_channels=_split(args.reset_channel),
                use_store=not args.no_store,
                kind=kind,
            )
        except Exception as e:
            print(f"❌  {target['display_name']}: {type(e).__name__}: {e}")
            failures += 1
    return 1 if failures else 0


# ── digest ───────────────────────────────────────────────────────

def cmd_digest(args) -> int:
    # Imported lazily so `scrape` still works if the LLM config is broken.
    from digest import LLMError, run as run_digest

    kind = "person" if args.person else "company"
    failures = 0
    for company in _companies(args):
        target = _resolve(args, company)
        _banner(f"DIGEST · {target['display_name']}")
        try:
            run_digest(
                company,
                new_only=not args.all_posts,
                since_days=args.since_days,
                cap=args.max_posts,
                out_dir=args.out_dir,
                store_path_override=_store_path(args, company),
                kind=kind,
                suggest_actions=getattr(args, "suggest_actions", False),
                profiles_only=getattr(args, "profiles_only", False),
            )
        except (FileNotFoundError, RuntimeError, LLMError) as e:
            print(f"❌  {target['display_name']}: {e}")
            failures += 1
    return 1 if failures else 0


# ── run (scrape then digest) ─────────────────────────────────────

async def cmd_run(args) -> int:
    scrape_code = await cmd_scrape(args)
    digest_code = cmd_digest(args)
    return scrape_code or digest_code


# ── status ───────────────────────────────────────────────────────

def cmd_status(args) -> int:
    from digest import describe_config

    _banner("STATUS")
    print(f"  LLM: {describe_config()}\n")

    header = f"  {'ACCOUNT':<24}{'POSTS':>6}{'NEW':>5}{'CH':>4}   {'LAST SCRAPE':<18}DIGEST"
    print(header)
    print("  " + "─" * (len(header) - 2))

    table = _table(args)
    for company in sorted(table):
        target = _resolve(args, company)
        path = _store_path(args, company)
        if not os.path.exists(path):
            print(f"  {target['display_name'][:23]:<24}{'—':>6}{'—':>5}{'—':>4}   not scraped yet")
            continue

        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        meta = doc.get("metadata", {})
        last = (doc.get("store", {}).get("last_run") or "")[:16].replace("T", " ")

        digest_path = os.path.join(args.out_dir, f"{target['key']}_digest.json")
        digest_state = "none"
        if os.path.exists(digest_path):
            with open(digest_path, encoding="utf-8") as fh:
                digest_state = json.load(fh).get("generated_at", "")[:16].replace("T", " ")

        print(
            f"  {target['display_name'][:23]:<24}"
            f"{meta.get('total_posts', 0):>6}"
            f"{meta.get('new_last_run', 0):>5}"
            f"{len(doc.get('data', {})):>4}   "
            f"{last:<18}{digest_state}"
        )

    print("\n  Channels per account:")
    for company in sorted(table):
        path = _store_path(args, company)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        counts = "  ".join(
            f"{ch}:{blk.get('count', 0)}"
            for ch, blk in sorted(doc.get("data", {}).items())
            if isinstance(blk, dict)
        )
        print(f"    {_resolve(args, company)['key']:<16}{counts}")
    print()
    return 0


# ── db-backfill ─────────────────────────────────────────────────

def cmd_db_backfill(args) -> int:
    """Sync every existing JSON output file into Postgres.

    Normal scrape/digest/save-target runs already mirror themselves to the
    database as they happen (see db.py) — this is only needed once, the
    first time DATABASE_URL is set, to backfill data collected before the
    database existed. Safe to re-run any time; every write is an upsert.
    """
    from config import DATABASE_URL
    from paths import DIGEST_DIR, store_path, digest_path
    import db

    if not DATABASE_URL:
        print("❌  DATABASE_URL is not set in .env — nothing to backfill to.")
        return 1

    _banner("DB BACKFILL")

    target_count = post_count = digest_count = 0
    for kind, table, resolve_fn in (
        ("company", COMPANY_TARGETS, resolve_company),
        ("person", PEOPLE_TARGETS, resolve_person),
    ):
        for key in sorted(table):
            target = resolve_fn(key)
            db.upsert_target(kind, target)
            target_count += 1

            store_file = store_path(key)
            posts_here = 0
            if os.path.exists(store_file):
                with open(store_file, encoding="utf-8") as fh:
                    doc = json.load(fh)
                db.upsert_posts(key, kind, doc.get("data", {}))
                posts_here = doc.get("metadata", {}).get("total_posts", 0)
                post_count += posts_here

            has_digest = False
            digest_file = digest_path(key)
            if os.path.exists(digest_file):
                with open(digest_file, encoding="utf-8") as fh:
                    digest = json.load(fh)
                db.upsert_digest(key, kind, digest)
                digest_count += 1
                has_digest = True

            print(f"  {target['display_name']:<40}{posts_here:>5} posts   digest: {'yes' if has_digest else 'no'}")

    history_file = os.path.join(OUTPUT_DIR, "run_history.json")
    history_count = 0
    if os.path.exists(history_file):
        with open(history_file, encoding="utf-8") as fh:
            entries = json.load(fh)
        for entry in entries:
            db.insert_run_history(entry)
            history_count += 1

    print(
        f"\n  Synced {target_count} targets, {post_count} posts, "
        f"{digest_count} digests, {history_count} run-history rows.\n"
    )
    return 0


# ── serve ────────────────────────────────────────────────────────

def _upsert_manifest_entry(kind: str, target: dict) -> None:
    """Add or update one target in frontend/manifest.json.

    Called after targets.save()/people_targets.save() so a target saved
    from the Run Pipeline page shows up in the sidebar's Accounts/People
    list immediately, not just in the CLI's target registry.
    """
    manifest_path = os.path.join(PROJECT_ROOT, "frontend", "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        manifest = {}
    manifest.setdefault("accounts", [])
    manifest.setdefault("people", [])

    section = "people" if kind == "person" else "accounts"
    key = target["key"]
    entry = {
        "key": key,
        "name": target.get("display_name", key),
        "file": f"../output/stores/{key}_output.json",
        "digest": f"../output/digests/{key}_digest.json",
    }
    if kind != "person":
        entry["ticker"] = target.get("ticker") or "—"

    manifest[section] = [e for e in manifest[section] if e.get("key") != key] + [entry]

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)


def cmd_serve(args) -> int:
    """Serve the frontend on localhost only.

    Bound to 127.0.0.1 deliberately: this directory holds .env with API
    tokens, and the default bind would expose it to the whole network.

    Threaded so a long-running /api/run POST (a full scrape can take
    minutes) doesn't block the page from loading stores/history meanwhile.
    """
    import http.server
    import socketserver
    import time

    class Handler(http.server.SimpleHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _read_json_file(self, path: str):
            if not os.path.exists(path):
                return None
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return None

        def _require_api_key(self) -> bool:
            """Gate on X-API-Key when API_KEY is set in .env; no-op locally.

            Applies to every /api/* route (GET and POST) — /api/run can
            trigger billed Apify scrapes and /api/send-email sends real
            mail, so once this process is reachable by anything other than
            the operator, those need a real check, not just obscurity.
            """
            from config import API_KEY

            if not API_KEY:
                return True
            if self.headers.get("X-API-Key") == API_KEY:
                return True
            self._send_json(401, {"ok": False, "error": "Missing or invalid X-API-Key header"})
            return False

        # Everything else on disk (.env, *.py source, custom_targets.json,
        # .git/, ...) is deliberately NOT reachable over HTTP — this list is
        # the complete set of what's safe to serve publicly.
        _PUBLIC_PREFIXES = ("/frontend/", "/output/")
        _PUBLIC_EXACT = {"/API.md"}

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            if path.startswith("/api/"):
                if not self._require_api_key():
                    return
                if path == "/api/accounts":
                    self._handle_list("company")
                elif path.startswith("/api/accounts/"):
                    self._handle_detail("company", path[len("/api/accounts/"):])
                elif path == "/api/people":
                    self._handle_list("person")
                elif path.startswith("/api/people/"):
                    self._handle_detail("person", path[len("/api/people/"):])
                else:
                    self.send_error(404)
                return

            if path.startswith(self._PUBLIC_PREFIXES) or path in self._PUBLIC_EXACT:
                super().do_GET()
                return

            self.send_error(404)

        def _handle_list(self, kind: str):
            from targets import COMPANY_TARGETS
            from people_targets import PEOPLE_TARGETS
            from paths import store_path, digest_path
            import db

            table = dict(PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS)
            # Targets that exist in Postgres but were never registered here
            # (e.g. an ad-hoc /api/run target, scraped but never explicitly
            # saved via /api/save-target) — DB-unreachable is a silent no-op.
            for row in db.list_targets(kind):
                table.setdefault(row["key"], row["config"])

            items = []
            for key in sorted(table):
                cfg = table[key]
                store = self._read_json_file(store_path(key))
                meta = (store or {}).get("metadata", {})
                has_store = store is not None
                has_digest = os.path.exists(digest_path(key))
                total_posts = meta.get("total_posts")
                last_run = (store or {}).get("store", {}).get("last_run")

                # No local JSON file (e.g. a freshly deployed instance that
                # hasn't scraped this target itself) — fall back to Postgres,
                # which may have it from wherever it was originally scraped.
                if store is None:
                    summary = db.get_summary(key)
                    if summary:
                        has_store = True
                        total_posts = summary["total_posts"]
                        last_run = summary["last_run"]
                if not has_digest and db.has_digest(key):
                    has_digest = True

                items.append({
                    "key": key,
                    "display_name": cfg.get("display_name", key),
                    "ticker": cfg.get("ticker") if kind == "company" else None,
                    "channels": _configured_channels(cfg),
                    "has_store": has_store,
                    "has_digest": has_digest,
                    "total_posts": total_posts,
                    "last_run": last_run,
                })
            list_key = "people" if kind == "person" else "accounts"
            self._send_json(200, {list_key: items})

        def _handle_detail(self, kind: str, raw_key: str):
            import urllib.parse
            from targets import resolve as resolve_company
            from people_targets import resolve as resolve_person
            from paths import store_path, digest_path
            import db

            key = urllib.parse.unquote(raw_key).strip().strip("/")
            if not key:
                self._send_json(404, {"ok": False, "error": "Missing key in URL"})
                return

            resolve_fn = resolve_person if kind == "person" else resolve_company
            try:
                target = resolve_fn(key)
            except KeyError as e:
                # Not registered in targets.py/people_targets.py/
                # custom_targets.json — check Postgres before giving up
                # (an ad-hoc /api/run target that was scraped but never
                # explicitly saved still has a row there).
                target = next(
                    (dict(row["config"], key=row["key"]) for row in db.list_targets(kind) if row["key"] == key),
                    None,
                )
                if target is None:
                    # KeyError.__str__ reprs its message (adds quotes) — unwrap it.
                    self._send_json(404, {"ok": False, "error": e.args[0] if e.args else str(e)})
                    return

            # No local JSON file (e.g. a freshly deployed instance that
            # hasn't scraped this target itself) — fall back to Postgres.
            # Reconstructed from `posts`/`digests` rows, so it won't be
            # byte-identical to the JSON file (no query/store metadata
            # block), but carries the same post/digest content.
            store = self._read_json_file(store_path(target["key"])) or db.get_store(target["key"])
            digest = self._read_json_file(digest_path(target["key"])) or db.get_digest(target["key"])

            self._send_json(200, {
                "target": target,
                "store": store,
                "digest": digest,
            })

        def do_POST(self):
            if not self._require_api_key():
                return
            if self.path == "/api/send-email":
                self._handle_send_email()
            elif self.path == "/api/run":
                self._handle_run()
            elif self.path == "/api/save-target":
                self._handle_save_target()
            else:
                self.send_error(404)

        def _handle_send_email(self):
            from mailer import send_email, MailerError

            try:
                payload = self._read_json_body()
                send_email(payload.get("to", ""), payload.get("subject", ""), payload.get("body", ""))
                self._send_json(200, {"ok": True})
            except MailerError as e:
                self._send_json(200, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(200, {"ok": False, "error": f"Unexpected error: {e}"})

        def _handle_run(self):
            import asyncio as _asyncio

            from engine import scrape_and_store
            from digest.pipeline import run as run_digest
            from digest.llm_client import LLMError
            import history

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            generate_digest = payload.get("generate_digest", True)

            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(400, {"ok": False, "error": "target must be a JSON object with a non-empty \"key\""})
                return
            target = dict(target)
            target["key"] = target["key"].strip()
            target.setdefault("display_name", target["key"])

            try:
                limit = int(payload.get("limit") or 10)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "limit must be a number"})
                return

            started = time.time()
            entry = {
                "kind": kind,
                "key": target["key"],
                "display_name": target["display_name"],
                "limit": limit,
            }
            try:
                stored = _asyncio.run(
                    scrape_and_store(limit=limit, kind=kind, target=target)
                )
                meta = stored.get("metadata", {})
                entry["new_posts"] = meta.get("new_last_run")
                entry["total_posts"] = meta.get("total_posts")
                entry["platforms_scraped"] = meta.get("platforms_scraped", [])
                entry["platforms_failed"] = meta.get("platforms_failed", [])

                if generate_digest:
                    try:
                        digest = run_digest(kind=kind, target=target)
                        entry["digest"] = {
                            "llm": digest.get("llm"),
                            "posts_considered": digest.get("posts_considered"),
                        }
                    except (LLMError, RuntimeError, FileNotFoundError) as e:
                        entry["digest"] = {"error": str(e)}

                entry["success"] = True
            except Exception as e:
                entry["success"] = False
                entry["error"] = str(e)

            entry["duration_ms"] = int((time.time() - started) * 1000)
            saved = history.record(entry)
            self._send_json(200, {"ok": entry["success"], "entry": saved})

        def _handle_save_target(self):
            import targets
            import people_targets

            try:
                payload = self._read_json_body()
            except json.JSONDecodeError:
                self._send_json(400, {"ok": False, "error": "Malformed JSON body"})
                return

            kind = "person" if payload.get("kind") == "person" else "company"
            target = payload.get("target")
            if not isinstance(target, dict) or not (target.get("key") or "").strip():
                self._send_json(
                    400,
                    {"ok": False, "error": 'target must be a JSON object with a non-empty "key"'},
                )
                return

            try:
                registry = people_targets if kind == "person" else targets
                saved = registry.save(target)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
                return

            _upsert_manifest_entry(kind, saved)
            self._send_json(200, {"ok": True, "target": saved})

        def do_DELETE(self):
            if not self._require_api_key():
                return
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/accounts/"):
                self._handle_delete("company", path[len("/api/accounts/"):])
            elif path.startswith("/api/people/"):
                self._handle_delete("person", path[len("/api/people/"):])
            else:
                self.send_error(404)

        def _handle_delete(self, kind: str, raw_key: str):
            """Remove a target: custom_targets.json entry (if any), local
            store/digest files, and its Postgres rows.

            Refuses to delete a target hand-written in targets.py/
            people_targets.py — those are source code, not data; removing
            one means editing the file, not calling this endpoint.
            """
            import urllib.parse
            import custom_targets
            import db
            from targets import COMPANY_TARGETS
            from people_targets import PEOPLE_TARGETS
            from paths import store_path, digest_path

            key = urllib.parse.unquote(raw_key).strip().strip("/")
            if not key:
                self._send_json(404, {"ok": False, "error": "Missing key in URL"})
                return

            section = "people" if kind == "person" else "companies"
            table = PEOPLE_TARGETS if kind == "person" else COMPANY_TARGETS
            in_custom = key in custom_targets.load_section(section)

            if key in table and not in_custom:
                source_file = "people_targets.py" if kind == "person" else "targets.py"
                self._send_json(400, {
                    "ok": False,
                    "error": f"'{key}' is a built-in target defined in {source_file} "
                    "— edit that file to remove it, this endpoint only removes "
                    "targets saved from the UI or scraped ad-hoc.",
                })
                return

            db_hit = any(row["key"] == key for row in db.list_targets(kind))
            if not in_custom and not db_hit:
                self._send_json(404, {"ok": False, "error": f"No such {kind} target: '{key}'"})
                return

            if in_custom:
                custom_targets.delete(section, key)
                table.pop(key, None)

            for path in (store_path(key), digest_path(key, "json"), digest_path(key, "md")):
                if os.path.exists(path):
                    os.remove(path)

            db.delete_target(kind, key)
            self._send_json(200, {"ok": True, "deleted": key})

    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True

    # Local default stays 127.0.0.1 — the path allowlist above and the
    # optional API_KEY gate make binding 0.0.0.0 safe (e.g. inside a
    # container, where SERVE_HOST=0.0.0.0 lets Docker's port mapping
    # reach it), but there's no reason to widen the local dev default.
    host = os.getenv("SERVE_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", args.port))

    from config import API_KEY
    import functools
    # Anchored to PROJECT_ROOT, not cwd — otherwise running `serve` from
    # outside this project's directory (e.g. a monorepo root one level up)
    # would serve whatever frontend/output happens to sit at that cwd
    # instead of this project's own.
    ServeHandler = functools.partial(Handler, directory=PROJECT_ROOT)
    try:
        with Server((host, port), ServeHandler) as httpd:
            _banner("SERVE")
            shown_host = "127.0.0.1" if host == "0.0.0.0" else host
            print(f"  Frontend:  http://{shown_host}:{port}/frontend/")
            print(
                "  Bound to localhost only, so .env stays private" if host == "127.0.0.1"
                else f"  Bound to {host} — only /frontend, /output, /API.md, and /api/* are servable"
            )
            print("  API_KEY required on /api/*" if API_KEY else "  API_KEY not set — /api/* is open to anyone who can reach this port")
            print("  Ctrl+C to stop\n")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except OSError as e:
        print(f"❌  Could not bind {host}:{port}: {e}")
        return 1
    return 0


# ── list ─────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    if args.person:
        _banner("CONFIGURED PEOPLE")
        for key in sorted(PEOPLE_TARGETS):
            t = resolve_person(key)
            print(f"\n  {t['display_name']}")
            print(f"    key        {key}")
            print(f"    linkedin   {t.get('linkedin_url') or '—'}")
            print(f"    twitter    {t.get('twitter_handle') or '—'}")
            print(f"    reddit     {t.get('reddit_query') or '—'}")
            print(f"    sec cik    {t.get('sec_cik') or '—'}")
            print(f"    news query {t.get('news_query') or '—'}")
            print(f"    patents    {t.get('patents_query') or '—'}")
        print("\n  Add or edit people in people_targets.py\n")
        return 0

    _banner("CONFIGURED ACCOUNTS")
    for key in sorted(COMPANY_TARGETS):
        t = resolve_company(key)
        print(f"\n  {t['display_name']}  ({t.get('ticker') or 'private'})")
        print(f"    key        {key}")
        print(f"    linkedin   {t['linkedin_url']}")
        print(f"    twitter    {t['twitter_handle']}")
        print(f"    sec cik    {t.get('sec_cik')}")
        print(f"    blog       {t['blog_url'][:76]}")
        print(f"    newsroom   {(t.get('newsroom_url') or '—')[:76]}")
    print("\n  Add or edit accounts in targets.py\n")
    return 0


# ── argument parsing ─────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Account intelligence pipeline: scrape → store → digest → serve.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py run --all --limit 20        full pipeline, every account\n"
            "  python main.py scrape bny --only sec,news  refresh the free channels only\n"
            "  python main.py digest northern_trust\n"
            "  python main.py status\n"
            "  python main.py serve --port 8001\n"
        ),
    )
    parser.add_argument(
        "--client",
        "-c",
        help="Client slug for multi-tenant data storage (e.g. stradit_india)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_selection(p):
        p.add_argument(
            "company",
            nargs="?",
            help=f"one of: {', '.join(sorted(COMPANY_TARGETS))}; "
            "or a person key with --person",
        )
        p.add_argument("--all", action="store_true", help="every configured account/person")
        p.add_argument(
            "--person",
            action="store_true",
            help="treat the target as a person from people_targets.py instead of a company",
        )
        p.add_argument(
            "--client",
            "-c",
            help="Client slug for multi-tenant data storage (e.g. stradit_india)",
        )

    def add_scrape_flags(p):
        p.add_argument("--limit", type=int, default=20, help="posts per channel (default 20)")
        p.add_argument("--only", help="channels to scrape, comma separated")
        p.add_argument("--reset-channel", help="channels to clear before merging")
        p.add_argument("--no-newsroom", action="store_true", help="skip press releases")
        p.add_argument("--no-store", action="store_true", help="overwrite instead of merging")

    def add_digest_flags(p):
        p.add_argument(
            "--all-posts",
            action="store_true",
            help="summarise the whole window, not only newly added posts",
        )
        p.add_argument("--since-days", type=int, default=14, help="recency window")
        p.add_argument("--max-posts", type=int, default=25, help="posts per channel")
        p.add_argument("--out-dir", default=DIGEST_DIR, help="digest output directory")
        p.add_argument(
            "--suggest-actions",
            action="store_true",
            help="person digests only: also LLM-suggest action items (written as "
                 "status=pending_review for a human to approve/reject) — see "
                 "ACTION_ITEMS_LLM_SUGGESTIONS_PLAN.md",
        )
        p.add_argument(
            "--profiles-only",
            action="store_true",
            help="person digests only: skip the email-rollup LLM call and just "
                 "generate the Personality Profile + Psychological Profile "
                 "(the per-channel summaries still run — they're required input "
                 "to those two profiles, not optional)",
        )

    p_scrape = sub.add_parser("scrape", help="scrape accounts into their stores")
    add_selection(p_scrape)
    add_scrape_flags(p_scrape)

    p_digest = sub.add_parser("digest", help="generate sales email + channel storylines")
    add_selection(p_digest)
    add_digest_flags(p_digest)

    p_run = sub.add_parser("run", help="scrape, then digest")
    add_selection(p_run)
    add_scrape_flags(p_run)
    add_digest_flags(p_run)

    p_status = sub.add_parser("status", help="what is stored, and how fresh it is")
    p_status.add_argument("--out-dir", default=DIGEST_DIR)
    p_status.add_argument("--person", action="store_true", help="show people instead of companies")
    p_status.add_argument("--client", "-c", help="Client slug for multi-tenant data storage (e.g. stradit_india)")

    p_backfill = sub.add_parser(
        "db-backfill",
        help="sync existing JSON output into Postgres (needs DATABASE_URL in .env)",
    )
    p_backfill.add_argument("--client", "-c", help="Client slug for multi-tenant data storage (e.g. stradit_india)")

    p_serve = sub.add_parser("serve", help="serve the frontend on localhost")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_serve.add_argument("--client", "-c", help="Client slug for multi-tenant data storage (e.g. stradit_india)")

    p_list = sub.add_parser("list", help="show configured accounts")
    p_list.add_argument("--person", action="store_true", help="show people instead of companies")
    p_list.add_argument("--client", "-c", help="Client slug for multi-tenant data storage (e.g. stradit_india)")
    return parser


def _dispatch_command(args) -> int:
    if args.command == "scrape":
        return asyncio.run(cmd_scrape(args))
    if args.command == "digest":
        return cmd_digest(args)
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "status":
        return cmd_status(args)
    if args.command == "db-backfill":
        return cmd_db_backfill(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "list":
        return cmd_list(args)
    return 1


def main() -> int:
    args = build_parser().parse_args()
    client_slug = getattr(args, "client", None)
    if client_slug:
        os.environ["CLIENT_SLUG"] = client_slug
        tenant_mod = None
        try:
            if "db.tenant" in sys.modules:
                tenant_mod = sys.modules["db.tenant"]
            elif "_root_db_tenant" in sys.modules:
                tenant_mod = sys.modules["_root_db_tenant"]
            else:
                import importlib.util
                curr_dir = os.path.dirname(os.path.abspath(__file__))
                root_path = os.path.abspath(os.path.join(curr_dir, "..", "..", "db", "tenant.py"))
                if os.path.isfile(root_path):
                    spec = importlib.util.spec_from_file_location("_root_db_tenant", root_path)
                    tenant_mod = importlib.util.module_from_spec(spec)
                    sys.modules["_root_db_tenant"] = tenant_mod
                    spec.loader.exec_module(tenant_mod)
        except Exception:
            tenant_mod = None

        if tenant_mod and hasattr(tenant_mod, "tenant_scope"):
            with tenant_mod.tenant_scope(client_slug):
                return _dispatch_command(args)

    return _dispatch_command(args)


if __name__ == "__main__":
    sys.exit(main())

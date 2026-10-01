#!/usr/bin/env python3
"""Kandji (Iru) -> Freshservice device ownership sync.

Kandji is the source of truth for who has which device. This compares each
Kandji device to the Freshservice asset with the same serial number and works
out which Freshservice owners/statuses are out of date.

Dry run by default. With --apply it updates only the owner ("Used By") and
status of Freshservice assets, reads each one back to verify, and never writes
to Kandji.

Credentials come from the environment (set in ~/.zshrc):
  KANDJI_API_URL, KANDJI_API_TOKEN, FRESHSERVICE_API_KEY,
  SLACK_DEVICE_SYNC_TOKEN (bot token with chat:write only)

Usage:
  sync.py                      dry run, print the plan and DM the summary
  sync.py --no-slack           same, but print the Slack message instead of sending
  sync.py --apply              make the changes (refused if owner changes exceed
                               max_owner_changes in config.json)
  sync.py --apply --only SERIAL [--only SERIAL ...]
                               make changes for just these serials
  sync.py --apply --max-owner-changes N
                               one-off override of the safety cap
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 30          # seconds per HTTP request
MAX_RETRIES = 3       # on 429 / 5xx
MAX_PAGES = 50        # hard cap on pagination loops


def http_request(method, url, headers, body=None):
    """HTTP call with bounded retries on rate limits and server errors.
    Returns parsed JSON, or None on 404."""
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if (e.code == 429 or e.code >= 500) and attempt < MAX_RETRIES:
                time.sleep(min(int(e.headers.get("Retry-After") or 5), 60))
                continue
            raise
    raise RuntimeError("unreachable")


def http_get(url, headers):
    return http_request("GET", url, headers)


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing environment variable {name} (expected in ~/.zshrc).")
    return value


def norm_serial(s):
    return (s or "").strip().upper()


def norm_email(s):
    return (s or "").strip().lower()


class Kandji:
    """Read-only by design: this client only exposes GET requests.
    The sync must never write to Kandji."""

    def __init__(self):
        url = require_env("KANDJI_API_URL").rstrip("/")
        if not url.startswith("http"):
            url = "https://" + url
        self.base = url
        self.headers = {"Authorization": "Bearer " + require_env("KANDJI_API_TOKEN")}

    def devices(self):
        out, limit = [], 300
        for page in range(MAX_PAGES):
            q = urllib.parse.urlencode({"limit": limit, "offset": page * limit})
            batch = http_get(f"{self.base}/api/v1/devices?{q}", self.headers) or []
            out += batch
            if len(batch) < limit:
                return out
        raise RuntimeError("Kandji pagination exceeded MAX_PAGES")


class Freshservice:
    def __init__(self, domain):
        self.base = f"https://{domain}/api/v2"
        token = base64.b64encode((require_env("FRESHSERVICE_API_KEY") + ":X").encode()).decode()
        self.headers = {"Authorization": "Basic " + token}
        self._email_by_id = {}
        self._id_by_email = {}

    def get(self, path):
        return http_get(self.base + path, self.headers)

    def update_asset(self, display_id, user_id=None, state=None, state_field=None):
        """Change only the owner and/or status of one asset, then read it back."""
        body = {}
        if user_id is not None:
            body["user_id"] = user_id
        if state is not None:
            body["type_fields"] = {state_field: state}
        http_request("PUT", f"{self.base}/assets/{display_id}", self.headers, body)
        return self.get(f"/assets/{display_id}?include=type_fields")["asset"]

    def assets(self):
        out = []
        for page in range(1, MAX_PAGES + 1):
            batch = self.get(f"/assets?per_page=100&page={page}&include=type_fields")["assets"]
            out += batch
            if len(batch) < 100:
                return out
        raise RuntimeError("Freshservice pagination exceeded MAX_PAGES")

    def email_for_user_id(self, user_id):
        """Asset owners can be requesters or agents (IT staff)."""
        if user_id is None:
            return None
        if user_id not in self._email_by_id:
            email = None
            r = self.get(f"/requesters/{user_id}")
            if r:
                email = r["requester"].get("primary_email")
            else:
                a = self.get(f"/agents/{user_id}")
                if a:
                    email = a["agent"].get("email")
            self._email_by_id[user_id] = norm_email(email) or None
        return self._email_by_id[user_id]

    def user_ids_for_email(self, email):
        """Return every matching Freshservice person id: requesters first, then agents."""
        email = norm_email(email)
        if email not in self._id_by_email:
            q = urllib.parse.quote(f"\"primary_email:'{email}'\"")
            ids = [r["id"] for r in (self.get(f"/requesters?query={q}") or {}).get("requesters", [])]
            if not ids:
                q = urllib.parse.urlencode({"email": email})
                ids = [a["id"] for a in (self.get(f"/agents?{q}") or {}).get("agents", [])]
            self._id_by_email[email] = ids
        return self._id_by_email[email]


def build_plan(cfg, kandji_devices, fs, fs_assets):
    serial_f, state_f = cfg["freshservice_serial_field"], cfg["freshservice_state_field"]
    type_ids = set(cfg["freshservice_asset_type_ids"])
    stock_owners = {norm_email(e) for e in cfg.get("stock_owner_emails", [])}

    fs_assets = [a for a in fs_assets if a["asset_type_id"] in type_ids]
    fs_by_serial, fs_dupes = {}, set()
    for a in fs_assets:
        s = norm_serial((a.get("type_fields") or {}).get(serial_f))
        if not s:
            continue
        if s in fs_by_serial:
            fs_dupes.add(s)
        fs_by_serial[s] = a

    k_counts = Counter(norm_serial(d.get("serial_number")) for d in kandji_devices)
    k_dupes = {s for s, n in k_counts.items() if s and n > 1}

    plan = {"owner_changes": [], "status_changes": [], "in_sync": 0,
            "report": {"no_kandji_user": [], "person_not_in_freshservice": [],
                       "ambiguous": [], "kandji_only": [], "freshservice_only": [],
                       "missing_in_kandji": [], "failed": []}}
    rep = plan["report"]
    seen = set()

    for d in kandji_devices:
        s = norm_serial(d.get("serial_number"))
        user = d.get("user") or {}
        email = norm_email(user.get("email"))
        label = {"serial": s, "device": d.get("device_name"), "model": d.get("model")}

        if d.get("is_missing"):
            rep["missing_in_kandji"].append({**label, "user": email or None,
                                             "last_check_in": (d.get("last_check_in") or "")[:10]})
        if not s:
            continue
        seen.add(s)
        if s in k_dupes or s in fs_dupes:
            rep["ambiguous"].append({**label, "why": "serial appears more than once"})
            continue
        asset = fs_by_serial.get(s)
        if asset is None:
            rep["kandji_only"].append({**label, "user": email or None})
            continue
        if not email:
            rep["no_kandji_user"].append(label)
            continue
        ids = fs.user_ids_for_email(email)
        if not ids:
            rep["person_not_in_freshservice"].append({**label, "user": email})
            continue
        if len(ids) > 1:
            rep["ambiguous"].append({**label, "why": f"{email} matches {len(ids)} Freshservice people"})
            continue

        target = ids[0]
        asset_ref = {**label, "asset_id": asset["display_id"], "asset_name": asset["name"]}
        changed = False
        if asset.get("user_id") != target:
            plan["owner_changes"].append({**asset_ref, "user_id": target, "to": email,
                                          "from": fs.email_for_user_id(asset.get("user_id"))})
            changed = True
        # Devices held by IT (spares on hand) are "In Stock"; everyone else's are "In Use".
        want = cfg["in_stock_state"] if email in stock_owners else cfg["in_use_state"]
        state = (asset.get("type_fields") or {}).get(state_f)
        if state != want:
            plan["status_changes"].append({**asset_ref, "from": state, "to": want})
            changed = True
        if not changed:
            plan["in_sync"] += 1

    # Retired assets are expected to be gone from Kandji; don't report them every run.
    # (If one is re-enrolled in Kandji it matches normally again.)
    quiet = set(cfg.get("quiet_states", []))
    for s, a in sorted(fs_by_serial.items()):
        if s not in seen and (a.get("type_fields") or {}).get(state_f) not in quiet:
            rep["freshservice_only"].append({"serial": s, "asset_id": a["display_id"],
                                             "asset_name": a["name"],
                                             "owner": fs.email_for_user_id(a.get("user_id"))})
    plan["counts"] = {"kandji_devices": len(kandji_devices), "freshservice_assets": len(fs_assets)}
    return plan


def print_plan(plan):
    c = plan["counts"]
    print(f"Kandji devices: {c['kandji_devices']} | Freshservice assets: {c['freshservice_assets']}")
    print(f"Already in sync: {plan['in_sync']}\n")

    print(f"OWNER CHANGES ({len(plan['owner_changes'])}):")
    for x in plan["owner_changes"]:
        print(f"  {x['serial']:<14} #{x['asset_id']:<4} {x['from'] or '(none)'} -> {x['to']}")
    print(f"\nSTATUS CHANGES ({len(plan['status_changes'])}):")
    moves = Counter(f"{x['from'] or '(blank)'} -> {x['to']}" for x in plan["status_changes"])
    for move, n in moves.items():
        print(f"  {move}: {n}")
    if not moves:
        print("  (none)")

    for key, title in REPORT_TITLES.items():
        items = plan["report"][key]
        if not items:
            continue
        print(f"\n{title} ({len(items)}):")
        for x in items:
            extra = x.get("user") or x.get("owner") or x.get("why") or ""
            when = f"  last check-in {x['last_check_in']}" if x.get("last_check_in") else ""
            print(f"  {x['serial']:<14} {x.get('model') or x.get('asset_name') or ''}  {extra}{when}")


REPORT_TITLES = {
    "person_not_in_freshservice": "Person not in Freshservice (not created)",
    "kandji_only": "In Kandji but no Freshservice asset (not created)",
    "freshservice_only": "In Freshservice but not in Kandji (owner left as-is)",
    "no_kandji_user": "No assigned user in Kandji (left as-is)",
    "ambiguous": "Ambiguous, skipped",
    "missing_in_kandji": "Marked missing in Kandji (not checking in)",
    "failed": "Update FAILED, check by hand",
}


def slack_text(plan, dry_run, note=""):
    """Plain Slack mrkdwn summary of one run."""
    head = "*Device sync" + (" (dry run, nothing changed)*" if dry_run else "*") + note
    verb = "Would change" if dry_run else "Changed"
    lines = [head]
    owners, statuses = plan["owner_changes"], plan["status_changes"]
    if not owners and not statuses:
        lines.append(f"All in sync: {plan['in_sync']} devices match Kandji.")
    else:
        lines.append(f"{plan['in_sync']} devices already match Kandji.")
    if owners:
        lines.append(f"\n*{verb} owner ({len(owners)}):*")
        lines += [f"• `{x['serial']}` #{x['asset_id']}: {x['from'] or '(none)'} → {x['to']}" for x in owners]
    if statuses:
        moves = Counter(f"{x['from'] or '(blank)'} → {x['to']}" for x in statuses)
        lines.append(f"\n*{verb} status ({len(statuses)}):* " + ", ".join(f"{m} ×{n}" for m, n in moves.items()))
    for key, title in REPORT_TITLES.items():
        items = plan["report"][key]
        if not items:
            continue
        lines.append(f"\n*{title} ({len(items)}):*")
        for x in items:
            who = x.get("user") or x.get("owner") or x.get("why") or ""
            when = f", last check-in {x['last_check_in']}" if x.get("last_check_in") else ""
            what = x.get("model") or x.get("asset_name") or ""
            lines.append(f"• `{x['serial']}` {what}: {who}{when}")
    return "\n".join(lines)


def send_slack_dm(user_id, text):
    """Bot DMs the user (shows in the app's Messages tab). Needs chat:write only."""
    body = json.dumps({"channel": user_id, "text": text, "unfurl_links": False}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=body, method="POST",
        headers={"Authorization": "Bearer " + require_env("SLACK_DEVICE_SYNC_TOKEN"),
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Slack error: {result.get('error')}")


def apply_plan(cfg, fs, plan, only):
    """Make the planned changes (optionally just for some serials), verifying each
    by reading the asset back. Returns a plan-shaped dict of what actually happened."""
    state_f = cfg["freshservice_state_field"]
    by_asset = {}
    for x in plan["owner_changes"]:
        by_asset.setdefault(x["asset_id"], {"owner": None, "status": None})["owner"] = x
    for x in plan["status_changes"]:
        by_asset.setdefault(x["asset_id"], {"owner": None, "status": None})["status"] = x
    if only:
        by_asset = {k: v for k, v in by_asset.items()
                    if (v["owner"] or v["status"])["serial"] in only}

    done = {**plan, "owner_changes": [], "status_changes": [],
            "report": {**plan["report"], "failed": []}}
    for asset_id, ch in by_asset.items():
        owner, status = ch["owner"], ch["status"]
        ref = owner or status
        try:
            after = fs.update_asset(asset_id,
                                    user_id=owner["user_id"] if owner else None,
                                    state=status["to"] if status else None,
                                    state_field=state_f)
            problems = []
            if owner and after.get("user_id") != owner["user_id"]:
                problems.append(f"owner reads back as {after.get('user_id')}")
            if status and (after.get("type_fields") or {}).get(state_f) != status["to"]:
                problems.append(f"status reads back as {(after.get('type_fields') or {}).get(state_f)}")
            if problems:
                raise RuntimeError("; ".join(problems))
            if owner:
                done["owner_changes"].append(owner)
            if status:
                done["status_changes"].append(status)
            print(f"  updated #{asset_id} {ref['serial']} (verified)")
        except Exception as e:  # keep going; every failure is reported
            done["report"]["failed"].append({"serial": ref["serial"], "asset_name": ref["asset_name"],
                                             "why": str(e)[:200]})
            print(f"  FAILED  #{asset_id} {ref['serial']}: {e}")
    return done


def main():
    ap = argparse.ArgumentParser(description="Kandji -> Freshservice device ownership sync")
    ap.add_argument("--apply", action="store_true", help="make changes (default: dry run)")
    ap.add_argument("--only", action="append", default=[], metavar="SERIAL",
                    help="with --apply: limit changes to this serial (repeatable)")
    ap.add_argument("--max-owner-changes", type=int, help="one-off override of the safety cap")
    ap.add_argument("--no-slack", action="store_true", help="print the Slack message instead of sending")
    args = ap.parse_args()
    only = {norm_serial(x) for x in args.only}
    if only and not args.apply:
        ap.error("--only only makes sense with --apply")

    cfg = json.loads((HERE / "config.json").read_text())
    kandji, fs = Kandji(), Freshservice(cfg["freshservice_domain"])
    plan = build_plan(cfg, kandji.devices(), fs, fs.assets())
    (HERE / "last_plan.json").write_text(json.dumps(plan, indent=2))

    def notify(text):
        if args.no_slack:
            print("\n--- Slack message (not sent) ---\n" + text)
        else:
            send_slack_dm(cfg["slack_user_id"], text)
            print("\nSlack summary sent.")

    if not args.apply:
        print("DRY RUN: nothing will be changed.\n")
        print_plan(plan)
        notify(slack_text(plan, dry_run=True))
        return

    cap = args.max_owner_changes if args.max_owner_changes is not None else cfg["max_owner_changes"]
    planned_owners = [x for x in plan["owner_changes"] if not only or x["serial"] in only]
    if len(planned_owners) > cap:
        msg = (f"*Device sync STOPPED, nothing changed.* It wanted to change {len(planned_owners)} "
               f"owners, more than the safety cap of {cap}. That usually means bad data or a "
               f"matching problem. Review with a dry run; if it's right, rerun with "
               f"`--apply --max-owner-changes {len(planned_owners)}`.")
        print(msg)
        notify(msg)
        sys.exit(2)

    if only:
        unknown = only - {x["serial"] for x in plan["owner_changes"] + plan["status_changes"]}
        if unknown:
            print(f"Nothing to change for: {', '.join(sorted(unknown))}")
    print("APPLYING changes" + (f" (only {', '.join(sorted(only))})" if only else "") + ":")
    done = apply_plan(cfg, fs, plan, only)
    note = f" (limited to {', '.join(sorted(only))})" if only else ""
    notify(slack_text(done, dry_run=False, note=note))
    if done["report"]["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

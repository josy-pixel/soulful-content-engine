#!/usr/bin/env python3
"""
setup_make.py
Auto-imports make-scenario.json into Make.com via the Make.com API.
Reads MAKE_API_KEY (and optional MAKE_ZONE) from .env.
"""

import json
import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY        = os.environ.get("MAKE_API_KEY", "").strip()
PREFERRED_ZONE = os.environ.get("MAKE_ZONE", "").strip()
BLUEPRINT_FILE = "make-scenario.json"

# All known Make.com zones to probe when MAKE_ZONE is not set or wrong
ALL_ZONES = ["eu1", "eu2", "us1", "us2", "us3"]


def make_headers(api_key):
    return {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }


def probe_zone(api_key):
    """Try each zone in turn; return the first one that returns HTTP 200."""
    zones = ([PREFERRED_ZONE] + ALL_ZONES) if PREFERRED_ZONE else ALL_ZONES
    for zone in zones:
        url = f"https://{zone}.make.com/api/v2/users/me"
        try:
            r = requests.get(url, headers=make_headers(api_key), timeout=10)
            if r.status_code == 200:
                return zone, r.json()
        except requests.RequestException:
            pass
    return None, None


def api_get(base, api_key, path, params=None):
    url = f"{base}/{path}"
    r = requests.get(url, headers=make_headers(api_key), params=params, timeout=15)
    if not r.ok:
        print(f"\n  [HTTP {r.status_code}] GET {url}")
        try:
            print(f"  {json.dumps(r.json(), indent=2)[:600]}")
        except Exception:
            print(f"  {r.text[:400]}")
        r.raise_for_status()
    return r.json()


def api_post(base, api_key, path, payload, params=None):
    url = f"{base}/{path}"
    r = requests.post(url, headers=make_headers(api_key), json=payload,
                      params=params, timeout=30)
    if not r.ok:
        print(f"\n  [HTTP {r.status_code}] POST {url}")
        try:
            print(f"  {json.dumps(r.json(), indent=2)[:800]}")
        except Exception:
            print(f"  {r.text[:600]}")
        r.raise_for_status()
    return r.json()


def sep():
    print("=" * 52)


def main():
    if not API_KEY:
        sys.exit("ERROR: MAKE_API_KEY is not set in .env")

    sep()
    print("  Soulful Content Engine -- Make.com Setup")
    sep()

    # ── 1. Detect zone + authenticate ─────────────────
    print("\n>> Detecting zone and authenticating...")
    zone, me_data = probe_zone(API_KEY)
    if not zone:
        sys.exit(
            "\nERROR: Could not authenticate on any Make.com zone.\n"
            "       Zones tried: " + ", ".join(ALL_ZONES) + "\n"
            "       Check that MAKE_API_KEY is correct and has 'scenarios:write' scope."
        )

    BASE = f"https://{zone}.make.com/api/v2"
    user = me_data.get("user", me_data)
    print(f"  [OK] Zone     : {zone}.make.com")
    print(f"  [OK] Logged in: {user.get('name','?')} <{user.get('email','?')}>")

    # Update .env zone so future runs skip probing
    if PREFERRED_ZONE != zone:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        try:
            with open(env_path, encoding="utf-8") as f:
                env_text = f.read()
            if "MAKE_ZONE=" in env_text:
                env_text = "\n".join(
                    f"MAKE_ZONE={zone}" if l.startswith("MAKE_ZONE=") else l
                    for l in env_text.splitlines()
                )
            else:
                env_text += f"\nMAKE_ZONE={zone}\n"
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(env_text)
            print(f"  [OK] Updated MAKE_ZONE={zone} in .env")
        except Exception:
            pass

    # ── 2. Find organisation ───────────────────────────
    print("\n>> Fetching organisation...")
    orgs = []
    try:
        orgs_data = api_get(BASE, API_KEY, "organizations")
        orgs = orgs_data.get("organizations", [])
    except requests.HTTPError:
        pass

    if not orgs:
        orgs = user.get("organizations", [])
        if isinstance(orgs, dict):
            orgs = [orgs]

    if not orgs:
        sys.exit(
            "\nERROR: No organisations found on this account.\n"
            "       Ensure the API key has the 'scenarios:write' scope in\n"
            "       Make.com -> Profile -> API -> your key -> Edit scopes."
        )

    org    = orgs[0]
    org_id = org.get("id") or org.get("organizationId")
    print(f"  [OK] Organisation: {org.get('name', org_id)} (ID: {org_id})")

    # ── 3. Find team ───────────────────────────────────
    print("\n>> Fetching team...")
    teams_data = api_get(BASE, API_KEY, "teams", params={"organizationId": org_id})
    teams = teams_data.get("teams", [])

    if not teams:
        sys.exit(
            "\nERROR: No teams found in this organisation.\n"
            "       Create a team in Make.com first, then re-run."
        )

    if len(teams) > 1:
        print("  Multiple teams found -- using first:")
        for i, t in enumerate(teams):
            print(f"    [{i}] {t.get('name','?')} (ID: {t.get('id')})")

    team    = teams[0]
    team_id = team.get("id")
    print(f"  [OK] Team: {team.get('name','?')} (ID: {team_id})")

    # ── 4. Load blueprint ──────────────────────────────
    print(f"\n>> Loading {BLUEPRINT_FILE}...")
    try:
        with open(BLUEPRINT_FILE, encoding="utf-8") as f:
            blueprint = json.load(f)
    except FileNotFoundError:
        sys.exit(f"\nERROR: {BLUEPRINT_FILE} not found. Run from the project root.")
    except json.JSONDecodeError as e:
        sys.exit(f"\nERROR: {BLUEPRINT_FILE} is invalid JSON: {e}")

    module_count = sum(
        1 + sum(len(r.get("flow", [])) for r in m.get("routes", []))
        for m in blueprint.get("flow", [])
    )
    print(f"  [OK] \"{blueprint['name']}\" ({module_count} modules)")

    # ── 5. Create scenario ─────────────────────────────
    print("\n>> Creating scenario in Make.com...")
    result = api_post(
        BASE, API_KEY,
        "scenarios",
        payload={
            "blueprint": blueprint,
            "scheduling": {"type": "indefinitely"},
            "teamId": team_id,
        },
        params={"confirmed": "true"},
    )

    scenario = result.get("scenario", result)
    sid   = scenario.get("id", "?")
    sname = scenario.get("name", blueprint["name"])

    # ── 6. Success ─────────────────────────────────────
    sep()
    print("  Scenario created successfully!")
    sep()
    print(f"  Name : {sname}")
    print(f"  ID   : {sid}")
    print(f"  URL  : https://{zone}.make.com/scenario/{sid}/edit")
    print()
    print("  Next steps:")
    print("  --------------------------------------------------")
    print("  1. Open the URL above")
    print("  2. Click the Webhook module (module 1)")
    print("     -> Create webhook -> copy the URL")
    print("     -> Set as MAKE_WEBHOOK_URL in .env and Render")
    print("  3. In each HTTP callback module (4, 6, 8, 10, 11):")
    print("     -> Replace YOUR_MAKE_WEBHOOK_SECRET")
    print("  4. Connect social accounts:")
    print("     -> Instagram Business  (module 3)")
    print("     -> Facebook Pages      (module 5)")
    print("     -> LinkedIn            (module 7)")
    print("  5. Set MAKE_WEBHOOK_SECRET in .env and Render")
    sep()


if __name__ == "__main__":
    main()

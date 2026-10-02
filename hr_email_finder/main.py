import argparse
import json
import sys

from hr_finder import find_hr_emails


def main():
    ap = argparse.ArgumentParser(description="Find publicly available HR/recruiting emails for a company.")
    ap.add_argument("company")
    ap.add_argument("--smtp-check", action="store_true", help="opt-in RCPT probe on inferred addresses")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = find_hr_emails(a.company, a.smtp_check, log=lambda m: print(m, file=sys.stderr))
    if a.json:
        print(json.dumps(res, indent=2))
        return
    if res.get("error"):
        sys.exit(res["error"])
    print(f"\n{res['company']}  ({res['domain_info']['domain']}), pattern: {res['pattern'].get('pattern') or 'unknown'}\n")
    for r in res["results"]:
        print(f"{r['kind']:17} {r['email']:40} {r['owner']}  {r.get('smtp', '')}\n{'':17} {r['source'] or r['note']}")


if __name__ == "__main__":
    main()

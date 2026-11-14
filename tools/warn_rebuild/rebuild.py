"""Own rebuild of the WARN combined file (Fable WARN audit 4A). Runs outside the engine.

Downloads every per-state export from biglocalnews/warn-github-flow at a pinned commit, runs
pinned warn-transformer with row-level tolerance, integrates against the PREVIOUS OWN BUILD
(so first_inserted_date stays stable between runs), and writes:
  integrated.csv, rescued.json, manifest.json   into --out

Guards (each fails the build, writing nothing, unless noted):
  * fewer rows than the previous build, or any previous hash_id missing;
  * a state needing more than MAX_RESCUED tolerated rows reuses its previous export
    (from --previous-manifest) instead of failing the build; no previous export -> state fails;
  * Wisconsin update rows ("UPDATE n - <name>[ * <note>]", jobs "69 (0)") are normalized to
    the original company and the current total; a plain-text "* <note>" suffix is dropped
    (names carrying the page's HTML before the asterisk stay verbatim, as earlier builds had
    them; a re-published notice reuses its earlier verbatim name), so integrate links them to
    the original notice as an amendment instead of inserting a new notice.

Usage:
  python rebuild.py --out DIR --previous PREV/integrated.csv \
      [--previous-manifest PREV/manifest.json]
Requires: warn-transformer==1.3.423 (pinned in requirements.txt), git on PATH.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

UTC = timezone.utc
REPO = "https://github.com/biglocalnews/warn-github-flow"
RAW = "https://raw.githubusercontent.com/biglocalnews/warn-github-flow/{commit}/data/warn-scraper/exports/{st}.csv"
TRANSFORMER_VERSION = "1.3.423"
MAX_RESCUED = 50
DATE = re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")
WI_PREFIX = re.compile(r"^\s*UPDATE\s*\d+\s*-\s*", re.I)


def get(url, limit=200_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "HeritageWarnRebuild/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response too large")
    return body


def branch_heads():
    out = subprocess.run(
        ["git", "ls-remote", "--heads", REPO], check=True, capture_output=True, text=True
    ).stdout
    heads = {}
    for line in out.splitlines():
        sha, ref = line.split()
        name = ref.removeprefix("refs/heads/")
        if re.fullmatch(r"[a-z]{2}", name):
            heads[name] = sha
    return heads


def wi_previous_names(previous_rows):
    """Earlier builds kept Wisconsin's old page HTML in names ("Durr CTS, Inc.<br/></a>...").
    Map each plain name to that verbatim form so a re-published notice keeps its hash."""
    names = {}
    for row in previous_rows:
        if row.get("postal_code") == "WI" and "<" in (row.get("company") or ""):
            names.setdefault(row["company"].split("<", 1)[0].strip().lower(), row["company"])
    return names


def normalize_wi(body, previous_names=None):
    """Return (normalized bytes, count) for Wisconsin's update-row format."""
    text = body.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        return body, 0
    fields = list(rows[0].keys())
    company = next(f for f in fields if f.strip().lower() == "company")
    changed = 0
    for row in rows:
        name = row[company] or ""
        clean = WI_PREFIX.sub("", name)
        head = clean.split("*", 1)[0]
        if "*" in clean and "<" not in head:
            # Plain-text "<name>* <note>" is the new page format; rows whose name carries the
            # page's HTML before the asterisk were already in earlier builds verbatim.
            clean = head
        clean = clean.strip()
        if clean != name.strip() and clean.lower() in (previous_names or {}):
            clean = previous_names[clean.lower()]
        if clean != name:
            row[company] = clean
            changed += 1
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8"), changed


class Tolerance:
    """Retry a row the stock code cannot transform once: first date / leading integer."""

    def __init__(self):
        from warn_transformer import schema

        self.B = schema.BaseTransformer
        self.orig = (self.B.transform_date, self.B.transform_jobs, self.B.transform_row)
        self.tolerant = False
        self.rescued = []

    def install(self):
        orig_date, orig_jobs, orig_row = self.orig
        tol = self

        def t_date(s, value):
            try:
                return orig_date(s, value)
            except (KeyError, ValueError, AssertionError):
                if not tol.tolerant:
                    raise
                m = DATE.search(str(value))
                if not m:
                    return None
                mo, d, y = (int(x) for x in m.groups())
                y += 2000 if y < 100 else 0
                try:
                    return datetime(y, mo, d).strftime("%Y-%m-%d")
                except ValueError:
                    return None

        def t_jobs(s, value):
            try:
                return orig_jobs(s, value)
            except (KeyError, ValueError, AssertionError):
                if not tol.tolerant:
                    raise
                m = re.match(r"\s*(\d[\d,]*)", str(value))
                return int(m.group(1).replace(",", "")) if m else None

        def t_row(s, row):
            try:
                return orig_row(s, row)
            except Exception as e:
                tol.tolerant = True
                try:
                    out = orig_row(s, row)
                    tol.rescued.append(
                        {
                            "state": s.postal_code,
                            "error": repr(e)[:120],
                            "company": out.get("company"),
                            "jobs": out.get("jobs"),
                            "notice_date": str(out.get("notice_date")),
                            "effective_date": str(out.get("effective_date")),
                        }
                    )
                    return out
                finally:
                    tol.tolerant = False

        self.B.transform_date, self.B.transform_jobs, self.B.transform_row = t_date, t_jobs, t_row


def read_previous(path):
    text = Path(path).read_bytes().decode("utf-8").replace("\x00", "")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def main(argv=None):
    cli = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--previous", type=Path, required=True)
    cli.add_argument("--previous-manifest", type=Path)
    cli.add_argument(
        "--heads", type=Path, help="JSON {state: commit} instead of git ls-remote (tests)"
    )
    args = cli.parse_args(argv)

    import warn_transformer
    from warn_transformer import integrate, utils

    version = getattr(warn_transformer, "__version__", None) or TRANSFORMER_VERSION
    previous_rows = read_previous(args.previous)
    previous_manifest = (
        json.loads(args.previous_manifest.read_text()) if args.previous_manifest else {}
    )
    heads = json.loads(args.heads.read_text()) if args.heads else branch_heads()

    work = Path(tempfile.mkdtemp(prefix="warn-rebuild-"))
    raw_dir, processed = work / "raw", work / "processed"
    raw_dir.mkdir()
    processed.mkdir()
    utils.WARN_TRANSFORMER_OUTPUT_DIR = work
    tol = Tolerance()
    tol.install()

    states, rows_all = {}, []
    for t in utils.get_all_transformers():
        st = t.lower()
        mod = import_module(f"warn_transformer.transformers.{t}")
        commit = heads.get(st)
        if not commit:
            raise SystemExit(f"no export branch for {st}")
        attempts = [("current", commit)]
        prev = (previous_manifest.get("states") or {}).get(st.upper())
        if prev and prev.get("export_commit") and prev["export_commit"] != commit:
            attempts.append(("previous", prev["export_commit"]))
        result = None
        for label, sha in attempts:
            body = get(RAW.format(commit=sha, st=st))
            normalized = 0
            if st == "wi":
                body, normalized = normalize_wi(body, wi_previous_names(previous_rows))
            (raw_dir / f"{st}.csv").write_bytes(body)
            before = len(tol.rescued)
            try:
                out = mod.Transformer(raw_dir).transform()
            except Exception as e:  # stock code failed even with tolerance
                result = dict(error=repr(e)[:200])
                continue
            rescued = len(tol.rescued) - before
            if rescued > MAX_RESCUED:
                del tol.rescued[before:]
                result = dict(error=f"{rescued} tolerated rows > {MAX_RESCUED}")
                continue
            result = dict(
                rows=out, used=label, commit=sha, rescued=rescued, wi_normalized=normalized
            )
            break
        if not result or "rows" not in result:
            raise SystemExit(f"{st.upper()} failed: {result}")
        rows_all += result["rows"]
        states[st.upper()] = {
            "export_commit": result["commit"],
            "export_url": RAW.format(commit=result["commit"], st=st),
            "used": result["used"],
            "rows": len(result["rows"]),
            "rescued": result["rescued"],
            **({"wi_normalized": result["wi_normalized"]} if st == "wi" else {}),
        }

    unique = list({d["hash_id"]: d for d in rows_all}.values())
    with open(processed / "consolidated.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, unique[0].keys())
        w.writeheader()
        w.writerows(unique)

    def own_previous(init=False):
        rows = read_previous(args.previous)
        for row in rows:
            row["last_updated_date"] = datetime.fromisoformat(row["last_updated_date"])
            row["first_inserted_date"] = datetime.fromisoformat(row["first_inserted_date"])
            row["estimated_amendments"] = int(row["estimated_amendments"])
        return rows

    integrate.get_current_data = own_previous
    path = integrate.run(processed / "consolidated.csv")
    body = Path(path).read_bytes()
    new_rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"), newline="")))
    new_ids, old_ids = {r["hash_id"] for r in new_rows}, {r["hash_id"] for r in previous_rows}
    if len(new_rows) < len(previous_rows) or not old_ids <= new_ids:
        raise SystemExit(
            f"Refusing: {len(new_rows)} rows vs previous {len(previous_rows)}, "
            f"{len(old_ids - new_ids)} previous hash_ids missing"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "integrated.csv").write_bytes(body)
    (args.out / "rescued.json").write_text(json.dumps(tol.rescued, indent=1))
    manifest = dict(
        built_at=datetime.now(UTC).isoformat(),
        transformer_version=version,
        row_count=len(new_rows),
        previous_row_count=len(previous_rows),
        added=len(new_ids - old_ids),
        rescued=len(tol.rescued),
        sha256=hashlib.sha256(body).hexdigest(),
        states=states,
    )
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps({k: v for k, v in manifest.items() if k != "states"}))
    return manifest


if __name__ == "__main__":
    main()

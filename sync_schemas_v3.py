#!/usr/bin/env python3
"""
v3 — Synchronise une API (ou toutes) avec le dossier unique des composants.

    python sync_schemas_v3.py <api.yaml>     une API (la dernière version de cette API)
    python sync_schemas_v3.py --all          la dernière version de chaque API

Pour chaque API, seules les références vers les composants sont mises à jour :
  1. $ref vers le fichier en bloc (…/rdv-actor-schemas.yaml#/components/<section>/X) -> fichier découpé ;
  2. chaque $ref vers un fichier découpé -> dernière version de la même majeure (1.0 -> 1.1).
Les schémas définis dans une API ne sont jamais touchés : seul extract_api_schemas_v3.py les extrait.
puis contrôle des références cassées et mise à jour des CHANGELOG.md. Une ancienne version d'API
n'est jamais modifiée.

--all n'analyse pas une API inchangée depuis sa dernière synchronisation réussie, tant qu'aucun composant
n'a reçu de nouvelle version (mémoire : <SCHEMAS_DIR>/.sync-state.json, régénérée si on la supprime).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common_v3 import (  # noqa: E402
    APIS_DIR, BLOCK_REF, SCHEMAS_DIR, SECTIONS, SPLIT_REF, dump, existing_versions, file_name, file_ref, load, map_refs, naming, relpath, schema_diff)

# ================================================================ catalogue (synchronisation)

class Catalog:
    """Composants existants du dossier unique : versions, section, contenu."""

    def __init__(self, out: Path):
        if not out.is_dir():
            raise ValueError(f"Dossier des composants introuvable : {out.resolve()} (lancez d'abord split_openapi_v3.py)")
        self.dir = out
        self.versions = {d.name: v for d in sorted(out.iterdir()) if d.is_dir() and (v := existing_versions(out, d.name))}
        self._docs: dict = {}

    def doc(self, name: str, version) -> dict:
        if (name, version) not in self._docs:
            self._docs[(name, version)] = load(self.dir / name / file_name(name, version))
        return self._docs[(name, version)]

    def section(self, name: str) -> str:
        components = self.doc(name, self.versions[name][-1]).get("components") or {}
        return next((s for s in SECTIONS if name in (components.get(s) or {})), "schemas")

    def content(self, name: str, version) -> dict:
        return (self.doc(name, version).get("components") or {}).get(self.section(name), {}).get(name)

    def latest(self, name: str, major: int | None = None) -> tuple[int, int] | None:
        versions = [v for v in self.versions.get(name, []) if major is None or v[0] == major]
        return versions[-1] if versions else None

    def ref(self, name: str, version, folder: str) -> str:
        return file_ref(name, version, self.section(name), folder)


def discover_apis(apis_dir: Path, out: Path) -> tuple[list[Path], list[Path]]:
    """(dernière version de chaque API <api>/v<x>/<api>.yaml, anciennes versions)."""
    if not apis_dir.is_dir():
        raise ValueError(f"Dossier des API introuvable : {apis_dir.resolve()} (adaptez APIS_DIR)")
    latest, older = [], []
    for api in sorted(p for p in apis_dir.iterdir() if p.is_dir() and p.resolve() != out.resolve()):
        versions = sorted((int(d.name[1:]), d / f"{api.name}.yaml") for d in api.iterdir()
                          if d.is_dir() and re.fullmatch(r"v\d+", d.name) and (d / f"{api.name}.yaml").exists())
        if versions:
            latest.append(versions[-1][1])
            older += [file for _, file in versions[:-1]]
    return latest, older


def check_latest(api: Path) -> None:
    """Refuse une API introuvable ou une ancienne version (une plus récente existe)."""
    if not api.is_file():
        raise ValueError(f"API introuvable : {api}")
    _, older = discover_apis(APIS_DIR, SCHEMAS_DIR)
    if api.resolve() in {file.resolve() for file in older}:
        raise ValueError(f"{display(api)} est une ancienne version d'API (une version plus récente existe) : non modifiée")


def display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(APIS_DIR.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def resolve_block_refs(doc, catalog: Catalog, api_dir: Path) -> tuple[dict, list[str]]:
    """$ref vers le fichier en bloc (…/rdv-actor-schemas.yaml#/components/<section>/X) -> fichier découpé
    (dernière version). Une cible inconnue reste telle quelle (signalée ensuite comme cassée si besoin)."""
    changes = []
    folder = Path(relpath(catalog.dir, api_dir))

    def replace(value: str, _) -> str:
        m = BLOCK_REF.match(value)
        if not m or SPLIT_REF.search(m["file"].replace("\\", "/")) or m["section"] not in SECTIONS:
            return value
        name = naming(m["section"], m["name"])
        version = catalog.latest(name)
        if not version:
            return value
        changes.append(f"{m['name']} (fichier commun) -> {name} v{version[0]}.{version[1]}")
        return catalog.ref(name, version, folder.as_posix())
    return map_refs(doc, replace), sorted(set(changes))


# ================================================================ changelog

CHANGELOG = "CHANGELOG.md"
_SECTION = re.compile(r"^## (\d+)\.(\d+)\b", re.M)
_CREATED_ON = re.compile(r"^# created on: (\S+)", re.M)
_HEADINGS = (("breaking", "⚠ Breaking changes"), ("added", "Added"), ("removed", "Removed"),
             ("changed", "Changed"), ("dependencies", "Updated dependencies"))


def update_changelog(catalog: Catalog, name: str) -> list[str]:
    """Ajoute à <Nom>/CHANGELOG.md les versions pas encore décrites (comparées à x.(y-1), ou à la dernière
    (x-1).y pour une majeure), du plus récent au plus ancien ; les notes écrites à la main sont gardées."""
    versions = catalog.versions.get(name, [])

    def previous(version):
        candidates = [v for v in versions if (v[0] == version[0] and v[1] < version[1])] if version[1] else \
                     [v for v in versions if v[0] < version[0]]
        return max(candidates) if candidates else None

    file = catalog.dir / name / CHANGELOG
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    documented = {(int(a), int(b)) for a, b in _SECTION.findall(text)}
    missing = [v for v in versions if v not in documented and previous(v)]
    if not missing:
        return []
    start = _SECTION.search(text)
    head = text[:start.start()] if start else f"# Changelog — {name}\n\nVersion history of `{name}`, most recent first.\n\n"
    sections = {(int(m[1]), int(m[2])): block.rstrip("\n") + "\n\n"
                for block in re.split(r"(?m)^(?=## )", text[start.start():] if start else "")
                if (m := _SECTION.match(block))}
    for version in missing:
        before = previous(version)
        diff = schema_diff(catalog.content(name, before), catalog.content(name, version))
        created = _CREATED_ON.search((catalog.dir / name / file_name(name, version)).read_text(encoding="utf-8"))
        lines = [f"## {version[0]}.{version[1]} — {created[1] if created else f'{date.today():%d/%m/%Y}'}"
                 + (" (major version)" if version[1] == 0 else ""), "", f"Compared to version {before[0]}.{before[1]}.", ""]
        for key, title in _HEADINGS:
            entries = diff["breaking"] if key == "breaking" else [e for e in diff[key] if e not in diff["breaking"]]
            if entries:
                lines += [f"### {title}", "", *[f"- {e}" for e in entries], ""]
        if not any(diff.values()):
            lines += ["No content change.", ""]
        sections[version] = "\n".join(lines) + "\n\n"
    body = "".join(sections[v] for v in sorted(sections, reverse=True))
    file.write_text(head.rstrip("\n") + "\n\n" + body.rstrip("\n") + "\n", encoding="utf-8")
    return [f"{major}.{minor}" for major, minor in missing]


# ================================================================ synchronisation

VERSIONED = re.compile(r"(?<=/)(?P<name>[^/\s'\"#]+)/(?P=name)_V(?P<major>\d+)\.(?P<minor>\d+)\.yaml")
RELATIVE = re.compile(r":\s*['\"]?(?P<ref>\.\.?/[^'\"\s#]+\.ya?ml)(?:#/components/(?P<section>[A-Za-z]+)/(?P<name>[^'\"\s]+))?")


def sync_api(api: Path, catalog: Catalog) -> tuple[list[str], list[str]]:
    """Synchronise une API : seules ses références vers des composants découpés sont mises à jour.
    Les schémas définis dans l'API ne sont jamais touchés (seul extract_api_schemas_v3.py les extrait).
    Rien n'est écrit en cas d'erreur. Renvoie (changements, erreurs)."""
    text = api.read_text(encoding="utf-8")
    try:
        doc = load(api)
    except ValueError as error:
        return [], [str(error)]
    doc, changes = resolve_block_refs(doc, catalog, api.parent)
    if changes:
        text = dump(doc)

    def upgrade(m: re.Match) -> str:
        name, major, minor = m["name"], int(m["major"]), int(m["minor"])
        newest = catalog.latest(name, major)
        if not newest or newest[1] <= minor:
            return m[0]
        changes.append(f"{name} : v{major}.{minor} -> v{major}.{newest[1]}")
        return f"{name}/{name}_V{major}.{newest[1]}.yaml"
    text = VERSIONED.sub(upgrade, text)
    if changes:
        api.write_text(text, encoding="utf-8")
    return changes, []


def broken_refs(files: list[Path]) -> list[str]:
    """$ref vers un fichier absent, ou vers un composant absent du fichier visé."""
    errors, known = [], {}
    for file in files:
        for m in RELATIVE.finditer(file.read_text(encoding="utf-8")):
            target = (file.parent / m["ref"]).resolve()
            if not target.exists():
                errors.append(f"{display(file)} : $ref introuvable -> {m['ref']}")
            elif m["name"]:
                if target not in known:
                    try:
                        known[target] = (load(target) or {}).get("components") or {}
                    except ValueError as error:
                        errors.append(str(error))
                        known[target] = None
                if known[target] is not None and m["name"] not in (known[target].get(m["section"]) or {}):
                    errors.append(f"{display(file)} : {m['section']}.{m['name']} absent de {m['ref']}")
    return list(dict.fromkeys(errors))


# Mémoire de la dernière synchronisation réussie : {API: empreinte du fichier + des versions disponibles}.
STATE = SCHEMAS_DIR / ".sync-state.json"


def fingerprint(api: Path, catalog: Catalog) -> str:
    """Change si l'API est modifiée ou si un composant reçoit une nouvelle version (ou en perd une)."""
    versions = json.dumps(sorted(catalog.versions.items()))
    return hashlib.sha256(api.read_bytes() + versions.encode()).hexdigest()


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run(apis: list[Path], title: str, skip_unchanged: bool = False) -> int:
    """skip_unchanged (--all) : une API inchangée depuis sa dernière synchronisation réussie, alors qu'aucun
    composant n'a changé de versions, n'est pas analysée."""
    catalog = Catalog(SCHEMAS_DIR)
    state = load_state()
    skipped = [api for api in apis if skip_unchanged and state.get(display(api)) == fingerprint(api, catalog)]
    print(f"Synchronisation de {len(apis)} API ({title}) :")
    apis = [api for api in apis if api not in skipped]
    if skipped:
        print(f"  · {len(skipped)} API inchangée(s) depuis la dernière synchronisation, non analysée(s)")
    updated, failed, current, succeeded = [], [], [], []
    for api in apis:
        changes, errors = sync_api(api, catalog)
        label = display(api)
        if errors:
            failed.append(api)
            print(f"  ✖ {label} : API non modifiée")
            for error in errors:
                print(f"      ✖ {error}")
            continue
        succeeded.append(api)
        if changes:
            updated.append(api)
            print(f"  ↑ {label} : mise à jour")
            for change in dict.fromkeys(changes):
                print(f"      ↑ {change}")
        else:
            current.append(api)
            if not skip_unchanged:
                print(f"  ✔ {label} : à jour")
    if skip_unchanged and current:
        print(f"  ✔ {len(current)} API à jour (aucun composant utilisé n'a changé)")
    broken = broken_refs(apis + [f for d in SCHEMAS_DIR.iterdir() if d.is_dir() for f in d.glob("*.yaml")])
    if broken:
        print("✖ Références cassées :")
        for error in broken:
            print(f"  ✖ {error}")
    logs = {name: added for name in catalog.versions if (added := update_changelog(catalog, name))}
    if logs:
        print("Changelog mis à jour :")
        for name, added in logs.items():
            print(f"  {name}/CHANGELOG.md : + {', '.join(added)}")
    # On ne mémorise que les API synchronisées sans erreur ni référence cassée : les autres seront réanalysées.
    for api in succeeded:
        if not any(error.startswith(display(api) + " :") for error in broken):
            state[display(api)] = fingerprint(api, catalog)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Bilan : {len(current)} à jour, {len(updated)} mise(s) à jour, "
          f"{len(failed)} en erreur"
          + (f", {len(skipped)} non analysée(s) (inchangées)" if skipped else "")
          + (f", {len(broken)} référence(s) cassée(s)" if broken else ""))
    return 1 if failed or broken else 0


def main() -> None:
    args = sys.argv[1:]
    try:
        if args == ["--all"]:
            latest, older = discover_apis(APIS_DIR, SCHEMAS_DIR)
            code = run(latest, "dernière version de chaque API", skip_unchanged=True)
            if older:
                print(f"  · anciennes versions ignorées : {', '.join(display(f.parent) for f in older)}")
        elif len(args) == 1 and not args[0].startswith("-"):
            api = Path(args[0])
            check_latest(api)
            code = run([api], "API choisie")
        else:
            sys.exit("Usage : python sync_schemas_v3.py <api.yaml>   ou   python sync_schemas_v3.py --all")
        sys.exit(code)
    except (ValueError, OSError) as error:
        sys.exit(f"✖ {error}")


if __name__ == "__main__":
    main()
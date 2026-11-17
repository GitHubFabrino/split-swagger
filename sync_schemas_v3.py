#!/usr/bin/env python3
"""
v3 — Synchronise une API (ou toutes) avec le dossier unique des composants.

    python sync_schemas_v3.py <api.yaml>     une API (la dernière version de cette API)
    python sync_schemas_v3.py --all          la dernière version de chaque API

Pour chaque API :
  1. $ref vers le fichier en bloc (…/rdv-actor-schemas.yaml#/components/<section>/X) -> fichier découpé ;
  2. schémas encore définis dans l'API, identiques à une version existante -> $ref vers cette version ;
  3. chaque $ref vers un fichier découpé -> dernière version de la même majeure ;
puis contrôle des références cassées et mise à jour des CHANGELOG.md. Une ancienne version d'API
n'est jamais modifiée.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common_v3 import (  # noqa: E402
    APIS_DIR, BLOCK_REF, LOCAL, SCHEMAS_DIR, SECTIONS, SPLIT_REF, api_name, dump, existing_versions, file_name,
    file_ref, load, map_refs, naming, relpath, renamed_for_api, schema_diff, schema_naming, shape, to_sibling_refs)

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


def externalize(doc: dict, catalog: Catalog, folder: str, label: str) -> tuple[dict, list[str], list[str]]:
    """Schémas définis dans l'API : remplacés par la version identique existante (nom normal, ou nom
    renommé avec l'API à l'extraction : Error -> ErrorRdvAdminActor), puis retirés de l'API."""
    inline = (doc.get("components") or {}).get("schemas") or {}
    if not inline:
        return doc, [], []
    renamed = {n: schema_naming(n)[0] for n in inline}
    alternative = {n: renamed_for_api(renamed[n], schema_naming(n)[1], api_name(label))[0] for n in inline}
    found: dict[str, tuple] = {}
    for _ in range(len(inline) + 1):            # un renommage change le contenu attendu des schémas qui l'utilisent
        found, changed = {}, False
        for name, schema in inline.items():
            target = shape(to_sibling_refs(map_refs(schema, lambda v, _: (
                file_ref(renamed[m["name"]], (0, 0), "schemas") if (m := LOCAL.match(v)) and m["name"] in inline
                else v))))
            for candidate in dict.fromkeys((renamed[name], alternative[name])):
                match = next((v for v in reversed(catalog.versions.get(candidate, []))
                              if shape(catalog.content(candidate, v)) == target), None)
                if match:
                    found[name] = (candidate, catalog.latest(candidate, match[0]))
                    changed |= candidate != renamed[name]
                    renamed[name] = candidate
                    break
        if not changed:
            break
    missing = sorted(set(inline) - set(found))
    if missing:
        return doc, [], [f"{n} défini dans l'API ne correspond à aucun composant existant "
                         f"(lancez extract_api_schemas_v3.py {label})" for n in missing]

    def replace(value: str, _) -> str:
        m = LOCAL.match(value)
        return catalog.ref(*found[m["name"]], folder) if m and m["section"] == "schemas" and m["name"] in found else value
    components = {k: v for k, v in doc["components"].items() if k != "schemas"}
    new = map_refs({**doc, "components": components}, replace)
    if not new["components"]:
        del new["components"]
    return new, [f"{n} -> {found[n][0]} v{found[n][1][0]}.{found[n][1][1]}" for n in sorted(found)], []


def sync_api(api: Path, catalog: Catalog) -> tuple[list[str], list[str]]:
    """Synchronise une API ; rien n'est écrit en cas d'erreur. Renvoie (changements, erreurs)."""
    label, text = display(api), api.read_text(encoding="utf-8")
    try:
        doc = load(api)
    except ValueError as error:
        return [], [str(error)]
    folder = relpath(catalog.dir, api.parent)
    doc, changes = resolve_block_refs(doc, catalog, api.parent)
    doc, inline_changes, errors = externalize(doc, catalog, folder, label)
    if errors:
        return [], errors
    changes += inline_changes
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


def run(apis: list[Path], title: str) -> int:
    catalog = Catalog(SCHEMAS_DIR)
    print(f"Synchronisation de {len(apis)} API ({title}) :")
    updated = failed = 0
    for api in apis:
        changes, errors = sync_api(api, catalog)
        if errors:
            failed += 1
            print(f"  ✖ {display(api)} : API non modifiée")
            for error in errors:
                print(f"      ✖ {error}")
        elif changes:
            updated += 1
            print(f"  ↑ {display(api)} : mise à jour")
            for change in dict.fromkeys(changes):
                print(f"      ↑ {change}")
        else:
            print(f"  ✔ {display(api)} : à jour")
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
    print(f"Bilan : {len(apis) - updated - failed} à jour, {updated} mise(s) à jour, {failed} en erreur"
          + (f", {len(broken)} référence(s) cassée(s)" if broken else ""))
    return 1 if failed or broken else 0


def main() -> None:
    args = sys.argv[1:]
    try:
        if args == ["--all"]:
            latest, older = discover_apis(APIS_DIR, SCHEMAS_DIR)
            code = run(latest, "dernière version de chaque API")
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
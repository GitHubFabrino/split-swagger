#!/usr/bin/env python3
"""
v3 — Synchronise une API (ou toutes) avec le dossier unique des composants.

    python sync_schemas_v3.py <api.yaml>     une API (la dernière version de cette API)
    python sync_schemas_v3.py --all          la dernière version de chaque API

Règle : chaque ref vers un composant découpé (dans l'API et dans les composants) passe à sa dernière version,
quel que soit son numéro, si elle est plus récente ; sinon rien ne change.
  1. composants découpés : leur dernière version est mise à jour sur place (pas de nouvelle version) —
     tous avec --all, ceux qu'utilise l'API sinon ;
  2. API : refs vers le fichier en bloc converties, refs mises à jour, et info.version (1.X.Y) montée si un
     composant utilisé a changé, directement ou à travers un autre : changement cassant (nouvelle majeure
     d'un composant) -> X+1, sinon Y+1 ;
puis contrôle des références cassées et CHANGELOG.md. Les schémas définis dans une API ne sont jamais touchés ;
une ancienne version d'API n'est jamais modifiée.
"""

from __future__ import annotations

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

    def latest(self, name: str) -> tuple[int, int] | None:
        versions = self.versions.get(name)
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


Move = tuple[str, tuple[int, int], tuple[int, int]]       # (composant, ancienne version, nouvelle version)


def upgrade_refs(text: str, catalog: Catalog) -> tuple[str, list[Move]]:
    """Chaque ref vers un composant découpé -> sa dernière version, si elle est plus récente (jamais en arrière)."""
    moves: list[Move] = []

    def upgrade(m: re.Match) -> str:
        name, current = m["name"], (int(m["major"]), int(m["minor"]))
        newest = catalog.latest(name)
        if not newest or newest <= current:
            return m[0]
        moves.append((name, current, newest))
        return f"{name}/{file_name(name, newest)}"
    return VERSIONED.sub(upgrade, text), moves


def text_moves(moves: list[Move]) -> list[str]:
    return [f"{n} : v{a[0]}.{a[1]} -> v{b[0]}.{b[1]}" for n, a, b in dict.fromkeys(moves)]


def closure(text: str, catalog: Catalog, names: set[str] = frozenset()) -> set[str]:
    """Composants utilisés par un texte (API) ou par des composants (names), directement ou à travers d'autres."""
    found = set()
    todo = [m["name"] for m in VERSIONED.finditer(text) if m["name"] in catalog.versions] + list(names)
    while todo:
        name = todo.pop()
        if name in found:
            continue
        found.add(name)
        latest = (catalog.dir / name / file_name(name, catalog.latest(name))).read_text(encoding="utf-8")
        todo += [m["name"] for m in VERSIONED.finditer(latest) if m["name"] in catalog.versions]
    return found


def refresh_components(catalog: Catalog, only: set[str] | None = None) -> dict[str, list[Move]]:
    """Dernière version de chaque composant découpé (ou de ceux de only) : ses refs passent à la dernière
    version des composants qu'il utilise, dans le fichier même (Callback_V1.0 : Consent v1.0 -> v1.1).
    Aucune nouvelle version n'est créée ; les versions plus anciennes ne sont pas touchées."""
    refreshed = {}
    for name, versions in sorted(catalog.versions.items()):
        if only is not None and name not in only:
            continue
        file = catalog.dir / name / file_name(name, versions[-1])
        text, moves = upgrade_refs(file.read_text(encoding="utf-8"), catalog)
        if moves:
            file.write_text(text, encoding="utf-8")
            refreshed[name] = moves
    return refreshed


INFO_VERSION_FLOW = re.compile(r"(?m)^info:[ \t]*\{[^}]*?\bversion:[ \t]*(['\"]?)(?P<v>[^,'\"}\s]+)\1")
INFO_VERSION = re.compile(r"(?m)^(info:[ \t]*\n(?:[ \t]+.*\n|[ \t]*\n)*?[ \t]+version:[ \t]*)(['\"]?)([^'\"\n#]+?)\2[ \t]*$")


def bump_version(text: str, breaking: bool) -> tuple[str, str | None]:
    """info.version au format 1.X.Y : changement cassant -> X+1 et Y=0 ; sinon Y+1. Renvoie (texte, « a -> b »)."""
    m = INFO_VERSION.search(text)
    group = 3
    if not m:
        m, group = INFO_VERSION_FLOW.search(text), "v"     # info: {title: …, version: 1.0.0}
    if not m:
        return text, None
    old = m[group].strip()
    parts = [int(p) if p.isdigit() else 0 for p in (old.split(".") + ["0", "0"])[:3]]
    new = [parts[0], parts[1] + 1, 0] if breaking else [parts[0], parts[1], parts[2] + 1]
    version = ".".join(map(str, new))
    return text[:m.start(group)] + version + text[m.end(group):], f"{old} -> {version}"


def sync_api(api: Path, catalog: Catalog, refreshed: dict[str, list[Move]],
             ignore: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Synchronise une API : ses refs vers les composants passent à leur dernière version ; info.version (1.X.Y)
    monte si un composant utilisé a changé, directement ou à travers un composant mis à jour (refreshed).
    Les schémas définis dans l'API ne sont jamais touchés. Rien n'est écrit en cas d'erreur.
    Renvoie (lignes à afficher, erreurs)."""
    text = api.read_text(encoding="utf-8")
    try:
        doc = load(api)
    except ValueError as error:
        return [], [str(error)]
    used_before = closure(text, catalog)       # les refs qui viennent d'être converties ne comptent pas
    doc, converted = resolve_block_refs(doc, catalog, api.parent)   # conversion technique : pas de montée de version
    if converted:
        text = dump(doc)
    text, moves = upgrade_refs(text, catalog)
    # ignore : composants que l'API vient d'extraire (et ce qu'ils utilisent) : son contrat n'a pas changé.
    indirect = {name: refreshed[name] for name in sorted(used_before - ignore) if name in refreshed}
    lines = converted + text_moves(moves) + [f"via {name} : {', '.join(text_moves(m))}" for name, m in indirect.items()]
    all_moves = moves + [move for m in indirect.values() for move in m]
    if all_moves:
        breaking = any(new[0] > old[0] for _, old, new in all_moves)
        text, bumped = bump_version(text, breaking)
        lines.insert(0, f"version {bumped}" + (" (changement cassant reçu)" if breaking else "") if bumped
                     else "⚠ info.version introuvable : version de l'API à monter à la main")
    if lines:
        api.write_text(text, encoding="utf-8")
    return lines, []


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


def run(apis: list[Path], title: str, everything: bool = False,
        extracted: dict[Path, set[str]] | None = None, created: set[str] = frozenset()) -> int:
    """everything (--all) : tous les composants découpés sont mis à jour ; sinon ceux qu'utilisent les API données,
    et les autres API qui utilisent un composant mis à jour (ou créé dans une nouvelle version : created) suivent.
    extracted (extraction) : {API: composants qu'elle vient d'extraire} -> pas de montée de version à cause d'eux."""
    extracted = {api.resolve(): names for api, names in (extracted or {}).items()}
    catalog = Catalog(SCHEMAS_DIR)
    used = None if everything else set().union(*(closure(api.read_text(encoding="utf-8"), catalog) for api in apis))
    refreshed = refresh_components(catalog, used)
    if refreshed:
        print(f"Schémas découpés mis à jour ({len(refreshed)}) :")
        for name, moves in refreshed.items():
            print(f"  ↑ {file_name(name, catalog.latest(name))[:-5]} : " + ", ".join(text_moves(moves)))
    also = []
    moved = set(refreshed) | set(created)
    if moved and not everything:              # un composant partagé a bougé : ses autres API suivent aussi
        latest, _ = discover_apis(APIS_DIR, SCHEMAS_DIR)
        chosen = {api.resolve() for api in apis}
        also = [api for api in latest if api.resolve() not in chosen
                and closure(api.read_text(encoding="utf-8"), catalog) & moved]
        apis = apis + also
    print(f"Synchronisation de {len(apis)} API ({title}" + (f" + {len(also)} qui utilisent les mêmes schémas"
                                                             if also else "") + ") :")
    updated, failed, current = [], [], []
    for api in apis:
        names = extracted.get(api.resolve(), set())
        lines, errors = sync_api(api, catalog, refreshed, closure("", catalog, names) if names else set())
        label = display(api)
        if errors:
            failed.append(api)
            print(f"  ✖ {label} : API non modifiée")
            for error in errors:
                print(f"      ✖ {error}")
        elif lines:
            updated.append(api)
            print(f"  ↑ {label} : mise à jour")
            for line in dict.fromkeys(lines):
                print(f"      ↑ {line}")
        else:
            current.append(api)
            if not everything:
                print(f"  ✔ {label} : à jour")
    if everything and current:
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
    print(f"Bilan : {len(current)} à jour, {len(updated)} mise(s) à jour, {len(failed)} en erreur"
          + (f", {len(broken)} référence(s) cassée(s)" if broken else ""))
    return 1 if failed or broken else 0


def main() -> None:
    args = sys.argv[1:]
    try:
        if args == ["--all"]:
            latest, older = discover_apis(APIS_DIR, SCHEMAS_DIR)
            code = run(latest, "dernière version de chaque API", everything=True)
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
#!/usr/bin/env python3
"""
Synchronise les $ref des API avec les schémas versionnés.

    <dossier des schémas>/<Nom>/<Nom>_V<x>.<y>.yaml  schémas (produits par split_openapi.py)
    <dossier des API>/<api>/v<x>/<api>.yaml         API (trouvées automatiquement)

Pour chaque API :
  1. $ref locales "#/components/schemas/E_Order_Input"
       -> "<chemin du dossier des schémas>/OrderInput/OrderInput_V<x.y>.yaml#/components/schemas/OrderInput"
     (même renommage que split_openapi.py : voir openapi_common.schema_naming)
     Le schéma défini dans l'API est comparé aux versions existantes pour trouver sa majeure,
     puis retiré du fichier (il vit désormais dans le dossier des schémas). S'il ne correspond à aucune
     version existante, l'API n'est pas modifiée : lancez d'abord split_openapi.py.
  2. $ref versionnées ".../OrderInput/OrderInput_V1.0.yaml" -> dernière version 1.y existante.
     Jamais vers une autre majeure (changement cassant).
Les fichiers de schémas ne sont jamais modifiés. Les extensions x-* sont laissées telles quelles.

Changelog (rédigé en anglais) : pour chaque schéma qui a au moins deux versions, <Nom>/CHANGELOG.md décrit
ce qui a changé entre chaque version et la précédente (added, removed, changed, dependencies, breaking).
Seules les versions pas encore décrites sont ajoutées ; le reste du fichier n'est pas touché.
Un schéma qui n'a qu'une version n'a pas de changelog.

Usage :
    python3 sync_schemas.py [api.yaml ...] [--schemas-dir D] [--apis-dir D] [--dry-run | --check | --watch [S]]

    Sans argument : SCHEMAS_DIR et APIS_DIR de openapi_common.py (configuration du projet).
    api.yaml ...   API à traiter (défaut : toutes les <dossier des API>/<api>/v<x>/<api>.yaml)
    --dry-run      affiche les changements sans rien écrire
    --check        n'écrit rien ; code 1 si une API n'est pas à jour ou si une $ref est cassée (CI)
    --watch        synchronise automatiquement à chaque modification (défaut : toutes les 2 s)

Dépendances : PyYAML (pip install pyyaml) et openapi_common.py, à garder à côté de ce script.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import (  # noqa: E402
    APIS_DIR, LOCAL, SCHEMAS_DIR, dump, load, loads, map_refs, rename_map, schema_diff, schema_refs, shape)

# Nom/Nom_V1.2.yaml (format actuel) ; Nom/Nom_v1.2.yaml (ancien format) reste reconnu
SCHEMA_FILE = re.compile(r"^(?P<name>.+)_(?P<v>[vV])(?P<major>\d+)\.(?P<minor>\d+)\.yaml$")
# …/Nom/Nom_V1.2.yaml, quel que soit le nom du dossier des schémas
VERSIONED_REF = re.compile(
    r"(?<=/)(?P<name>[^/\s'\"#]+)/(?P=name)_(?P<v>[vV])(?P<major>\d+)\.(?P<minor>\d+)\.yaml")
# Toute valeur qui est un chemin relatif vers un .yaml (+ fragment éventuel) : $ref et discriminator.mapping
RELATIVE_PATH = re.compile(
    r":\s*['\"]?(?P<ref>\.\.?/[^'\"\s#]+\.ya?ml)(?:#/components/schemas/(?P<schema>[^'\"\s]+))?")


# ================================================================ index des schémas

class Catalog:
    """Versions existantes dans le dossier des schémas : {Nom: {majeure: [mineures triées]}}."""

    def __init__(self, schemas_dir: Path):
        self.dir = schemas_dir
        if not self.dir.is_dir():
            raise ValueError(f"Dossier des schémas introuvable : {self.dir.resolve()} "
                             f"(lancez d'abord split_openapi.py, ou adaptez SCHEMAS_DIR dans openapi_common.py)")
        self.versions: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.letter: dict[str, str] = {}  # « V » (actuel) ou « v » (ancien format), par schéma
        for folder in (d for d in self.dir.iterdir() if d.is_dir()):
            for file in folder.iterdir():
                m = SCHEMA_FILE.match(file.name)
                if m and m["name"] == folder.name:
                    self.versions[folder.name][int(m["major"])].append(int(m["minor"]))
                    self.letter[folder.name] = m["v"]
        for majors in self.versions.values():
            for minors in majors.values():
                minors.sort()
        self._files: dict[tuple[str, int, int], dict] = {}

    def latest(self, name: str, major: int) -> int | None:
        minors = self.versions.get(name, {}).get(major)
        return minors[-1] if minors else None

    def all(self, name: str) -> list[tuple[int, int]]:
        return sorted((major, minor) for major, minors in self.versions.get(name, {}).items() for minor in minors)

    def file_name(self, name: str, major: int, minor: int) -> str:
        return f"{name}_{self.letter.get(name, 'V')}{major}.{minor}.yaml"

    def fragment(self, name: str) -> str:
        """Format actuel : le schéma est dans components.schemas.<Nom>, la $ref doit y pointer."""
        return f"#/components/schemas/{name}" if self.letter.get(name, "V") == "V" else ""

    def ref(self, name: str, major: int, minor: int) -> str:
        return self.file_name(name, major, minor) + self.fragment(name)

    def path(self, name: str, major: int, minor: int) -> Path:
        return self.dir / name / self.file_name(name, major, minor)

    def content(self, name: str, major: int, minor: int) -> dict:
        """Le schéma d'une version : components.schemas.<Nom> (format actuel) ou le fichier entier (ancien)."""
        key = (name, major, minor)
        if key not in self._files:
            doc = load(self.path(name, major, minor))
            schemas = (doc.get("components") or {}).get("schemas") or {} if isinstance(doc, dict) else {}
            self._files[key] = schemas.get(name, doc)
        return self._files[key]

    def files(self) -> list[Path]:
        return [f for d in self.dir.iterdir() if d.is_dir() for f in d.glob("*.yaml")]

    def matching_major(self, name: str, inline: dict, names: set, renamed: dict[str, str]) -> int | None:
        """Majeure de la version existante identique au schéma défini dans une API
        (name : nouveau nom ; renamed : {ancien nom: nouveau nom} des schémas de l'API)."""
        target = shape(map_refs(inline, names, lambda dep: f"../{renamed[dep]}/{self.ref(renamed[dep], 0, 0)}"))
        for major, minor in reversed(self.all(name)):
            if shape(self.content(name, major, minor)) == target:
                return major
        return None


# ================================================================ synchronisation d'une API

def display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def externalize(doc: dict, catalog: Catalog, prefix: str, label: str) -> tuple[dict | None, list[str], list[str]]:
    """Étape 1 : remplace #/components/schemas/E_X_Input par le fichier versionné XInput/XInput_V<x.y>.yaml
    et retire les schémas définis dans l'API. Renvoie (document modifié ou None si erreur, changements, erreurs)."""
    inline = (doc.get("components") or {}).get("schemas") or {}
    names = set(inline)
    used = schema_refs(doc, names) | names
    try:
        renamed = rename_map(used)
    except ValueError as error:
        return None, [], [f"{label} : {error}"]
    majors: dict[str, int] = {}
    errors = []
    for name in sorted(used):
        new = renamed[name]
        if not catalog.all(new):
            errors.append(f"{label} : schéma {name} ({new}) absent de schemas/ (lancez split_openapi.py)")
        elif name not in inline:
            errors.append(f"{label} : #/components/schemas/{name} référencé mais non défini dans l'API")
        elif (major := catalog.matching_major(new, inline[name], names, renamed)) is None:
            errors.append(f"{label} : {name} défini dans l'API ne correspond à aucune version de "
                          f"schemas/{new}/ (modifié ? lancez split_openapi.py sur le nouveau fichier)")
        else:
            majors[name] = major
    if errors:
        return None, [], errors

    def to_file(name: str) -> str:
        new, major = renamed[name], majors[name]
        return f"{prefix}/{new}/{catalog.ref(new, major, catalog.latest(new, major))}"

    components = {k: v for k, v in (doc.get("components") or {}).items() if k != "schemas"}
    new_doc = map_refs({**doc, "components": components}, names, to_file)
    if not new_doc["components"]:
        del new_doc["components"]
    changes = [f"{name} -> {renamed[name]} v{majors[name]}.{catalog.latest(renamed[name], majors[name])}"
               for name in sorted(majors)]
    if inline:
        changes.append(f"{len(inline)} schéma(s) retiré(s) de components.schemas")
    return new_doc, changes, []


def upgrade_versions(text: str, catalog: Catalog) -> tuple[str, list[str]]:
    """Étape 2 : chaque schemas/X/X_va.b.yaml -> dernière version a.y existante."""
    changes = []

    def upgrade(m: re.Match) -> str:
        name, major, minor = m["name"], int(m["major"]), int(m["minor"])
        newest = catalog.latest(name, major)
        if newest is None or newest <= minor:
            return m[0]
        changes.append(f"{name} : v{major}.{minor} -> v{major}.{newest}")
        return f"{name}/{name}_{m['v']}{major}.{newest}.yaml"

    return VERSIONED_REF.sub(upgrade, text), changes


def sync_api(api_file: Path, catalog: Catalog, write: bool, root: Path) -> tuple[list[str], list[str]]:
    """Synchronise une API. Renvoie (changements, erreurs) ; rien n'est écrit en cas d'erreur."""
    label = display(api_file, root)
    text = api_file.read_text(encoding="utf-8")
    changes: list[str] = []

    doc = loads(text) if LOCAL in text or "components" in text else None
    has_inline = bool(((doc or {}).get("components") or {}).get("schemas"))
    if doc is not None and (has_inline or LOCAL in text):
        prefix = os.path.relpath(catalog.dir, api_file.parent).replace(os.sep, "/")
        new_doc, changes, errors = externalize(doc, catalog, prefix, label)
        if errors:
            return [], errors
        if changes:
            text = dump(new_doc)

    text, upgraded = upgrade_versions(text, catalog)
    changes += upgraded
    if write and changes:
        api_file.write_text(text, encoding="utf-8")
    return changes, []


# ================================================================ changelog

CHANGELOG = "CHANGELOG.md"
_SECTION = re.compile(r"^## (\d+)\.(\d+)\b", re.M)
_CREATED_ON = re.compile(r"^# created on: (\S+)", re.M)
_HEADINGS = (
    ("breaking", "⚠ Breaking changes"),
    ("added", "Added"),
    ("removed", "Removed"),
    ("changed", "Changed"),
    ("dependencies", "Updated dependencies"),
)


def previous_version(versions: list[tuple[int, int]], version: tuple[int, int]) -> tuple[int, int] | None:
    """Version à laquelle comparer : x.(y-1) pour une mineure, la dernière (x-1).y pour une majeure."""
    major, minor = version
    if minor > 0:
        same_line = [v for v in versions if v[0] == major and v[1] < minor]
        return max(same_line) if same_line else None
    lower = [v for v in versions if v[0] < major]
    return max(lower) if lower else None


def changelog_section(catalog: Catalog, name: str, version: tuple[int, int], previous: tuple[int, int]) -> str:
    """Section « ## x.y — date » décrivant les changements depuis la version précédente."""
    diff = schema_diff(catalog.content(name, *previous), catalog.content(name, *version))
    created = _CREATED_ON.search(catalog.path(name, *version).read_text(encoding="utf-8"))
    when = created[1] if created else f"{date.today():%d/%m/%Y}"
    lines = [f"## {version[0]}.{version[1]} — {when}" + (" (major version)" if version[1] == 0 else ""),
             "", f"Compared to version {previous[0]}.{previous[1]}.", ""]
    breaking = diff["breaking"]
    for key, title in _HEADINGS:
        # un changement cassant n'est listé qu'une fois, dans sa propre rubrique
        items = breaking if key == "breaking" else [item for item in diff[key] if item not in breaking]
        if items:
            lines += [f"### {title}", "", *[f"- {item}" for item in items], ""]
    if not any(diff.values()):
        lines += ["No content change.", ""]
    return "\n".join(lines) + "\n"


def update_changelog(catalog: Catalog, name: str, write: bool) -> list[str]:
    """Ajoute à <Nom>/CHANGELOG.md les versions pas encore décrites. Renvoie ces versions."""
    versions = catalog.all(name)
    if len(versions) < 2:
        return []
    file = catalog.dir / name / CHANGELOG
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    documented = {(int(a), int(b)) for a, b in _SECTION.findall(text)}
    missing = [v for v in versions if v not in documented and previous_version(versions, v)]
    if not missing:
        return []

    # Le fichier est découpé en en-tête + une section par version, pour garder les sections
    # existantes intactes (y compris des notes ajoutées à la main) et l'ordre du plus récent au plus ancien.
    start = _SECTION.search(text)
    head = text[: start.start()] if start else (
        f"# Changelog — {name}\n\nVersion history of `{name}`, most recent first.\n\n")
    sections = {}
    for block in re.split(r"(?m)^(?=## )", text[start.start():] if start else ""):
        m = _SECTION.match(block)
        if m:
            sections[(int(m[1]), int(m[2]))] = block.rstrip("\n") + "\n\n"
    for version in missing:
        sections[version] = changelog_section(catalog, name, version, previous_version(versions, version)) + "\n"
    if write:
        body = "".join(sections[v] for v in sorted(sections, reverse=True))
        file.write_text(head.rstrip("\n") + "\n\n" + body.rstrip("\n") + "\n", encoding="utf-8")
    return [f"{major}.{minor}" for major, minor in sorted(missing)]


# ================================================================ découverte et contrôles

def discover_apis(apis_dir: Path, schemas_dir: Path) -> list[Path]:
    """Toutes les <dossier des API>/<api>/v<x>/<api>.yaml (le dossier des schémas est ignoré)."""
    if not apis_dir.is_dir():
        raise ValueError(f"Dossier des API introuvable : {apis_dir.resolve()} (adaptez APIS_DIR dans openapi_common.py)")
    found = []
    for api in sorted(p for p in apis_dir.iterdir() if p.is_dir() and p.resolve() != schemas_dir.resolve()):
        for version_dir in sorted(p for p in api.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)):
            file = version_dir / f"{api.name}.yaml"
            if file.exists():
                found.append(file)
    return found


def broken_refs(files: list[Path], root: Path) -> list[str]:
    """Références relatives ($ref, discriminator.mapping) vers un fichier absent, ou vers un
    schéma absent de components.schemas du fichier visé."""
    errors = []
    schemas_in: dict[Path, set] = {}
    for file in files:
        for m in RELATIVE_PATH.finditer(file.read_text(encoding="utf-8")):
            target = (file.parent / m["ref"]).resolve()
            if not target.exists():
                errors.append(f"{display(file, root)} : $ref introuvable -> {m['ref']}")
                continue
            if m["schema"]:
                if target not in schemas_in:
                    doc = load(target)
                    schemas_in[target] = set(((doc or {}).get("components") or {}).get("schemas") or {})
                if m["schema"] not in schemas_in[target]:
                    errors.append(f"{display(file, root)} : schéma {m['schema']} absent de {m['ref']}")
    return list(dict.fromkeys(errors))


def run(schemas_dir: Path, apis_dir: Path, apis: list[Path], mode: str) -> int:
    """mode : write | dry-run | check. Renvoie le code de sortie."""
    catalog = Catalog(schemas_dir)
    targets = apis or discover_apis(apis_dir, schemas_dir)
    root = apis_dir
    changes: dict[str, list[str]] = {}
    errors: list[str] = []
    for api in targets:
        api_changes, api_errors = sync_api(api, catalog, mode == "write", root)
        if api_changes:
            changes[display(api, root)] = api_changes
        errors += api_errors
    if mode in ("write", "check"):
        errors += broken_refs(targets + catalog.files(), root)
    changelogs = {name: added for name in sorted(catalog.versions)
                  if (added := update_changelog(catalog, name, write=mode == "write"))}

    tag = {"dry-run": "[simulation] ", "check": "[contrôle] "}.get(mode, "")
    if changes:
        print(f"{tag}{'Mises à jour nécessaires' if mode == 'check' else 'Synchronisé'} ({len(targets)} API) :")
        for api, items in changes.items():
            print(f"  {api}")
            for item in dict.fromkeys(items):
                print(f"    ↑ {item}")
    elif not errors:
        print(f"{tag}✔ {len(targets)} API à jour avec les dernières versions des schémas")
    if changelogs:
        print(f"{tag}{'Changelog à compléter' if mode == 'check' else 'Changelog mis à jour'} :")
        for name, added in changelogs.items():
            print(f"  {name}/{CHANGELOG} : + {', '.join(added)}")
    if errors:
        print(f"{tag}✖ Erreurs :", file=sys.stderr)
        for error in dict.fromkeys(errors):
            print(f"  ✖ {error}", file=sys.stderr)
    return 1 if errors or (mode == "check" and (changes or changelogs)) else 0


# ================================================================ mode --watch

def snapshot(*folders: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for folder in folders for p in folder.rglob("*.yaml")}


def watch(schemas_dir: Path, apis_dir: Path, apis: list[Path], interval: float) -> None:
    print(f"Surveillance de {schemas_dir} et {apis_dir} (Ctrl+C pour arrêter)…")
    run(schemas_dir, apis_dir, apis, "write")
    before = snapshot(schemas_dir, apis_dir)
    try:
        while True:
            time.sleep(interval)
            if snapshot(schemas_dir, apis_dir) != before:
                print(f"\n[{time.strftime('%H:%M:%S')}] modification détectée")
                try:
                    run(schemas_dir, apis_dir, apis, "write")
                except (ValueError, OSError, yaml.YAMLError) as error:
                    print(f"✖ {error}", file=sys.stderr)
                before = snapshot(schemas_dir, apis_dir)  # inclut nos propres écritures
    except KeyboardInterrupt:
        print("\nArrêt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise les $ref des API avec les schémas versionnés.")
    parser.add_argument("apis", type=Path, nargs="*",
                        help="API à traiter (défaut : toutes les <dossier des API>/<api>/v<x>/<api>.yaml)")
    parser.add_argument("--schemas-dir", type=Path, default=SCHEMAS_DIR, help=f"défaut : {SCHEMAS_DIR}")
    parser.add_argument("--apis-dir", type=Path, default=APIS_DIR, help=f"défaut : {APIS_DIR}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="affiche sans écrire")
    mode.add_argument("--check", action="store_true", help="code 1 si quelque chose n'est pas à jour (CI)")
    mode.add_argument("--watch", nargs="?", type=float, const=2.0, metavar="SECONDES",
                      help="synchronise automatiquement à chaque modification")
    args = parser.parse_args()
    try:
        if args.watch is not None:
            watch(args.schemas_dir, args.apis_dir, args.apis, args.watch)
        else:
            mode = "dry-run" if args.dry_run else "check" if args.check else "write"
            sys.exit(run(args.schemas_dir, args.apis_dir, args.apis, mode))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
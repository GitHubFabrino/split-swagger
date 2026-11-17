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
    python3 sync_schemas.py [api.yaml ...] [--components-dir D] [--apis-dir D] [--dry-run | --check | --watch [S]]

    Sans argument : COMPONENTS_DIR et APIS_DIR de openapi_common.py (configuration du projet).
    api.yaml ...   API à traiter (défaut : la dernière version de chaque <dossier des API>/<api>/v<x>/<api>.yaml ;
                   les anciennes versions ne sont jamais modifiées)
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
    APIS_DIR, COMPONENTS_DIR, LOCAL, SECTIONS, api_name, block_ref, disk_name, dump, load, loads, map_refs,
    rename_map, renamed_for_api, schema_diff, to_sibling_refs, schema_naming, schema_refs, shape,
    yaml_error)

# Nom/Nom_V1.2.yaml (format actuel) ; Nom/Nom_v1.2.yaml (ancien format) reste reconnu
SCHEMA_FILE = re.compile(r"^(?P<name>.+)_(?P<v>[vV])(?P<major>\d+)\.(?P<minor>\d+)\.yaml$")
# …/Nom/Nom_V1.2.yaml, quel que soit le nom du dossier des schémas
VERSIONED_REF = re.compile(
    r"(?<=/)(?:(?P<section>[^/\s'\"#]+)/)?(?P<name>[^/\s'\"#]+)/(?P=name)_(?P<v>[vV])(?P<major>\d+)\.(?P<minor>\d+)\.yaml")
# Toute valeur qui est un chemin relatif vers un .yaml (+ fragment éventuel) : $ref et discriminator.mapping
RELATIVE_PATH = re.compile(
    r":\s*['\"]?(?P<ref>\.\.?/[^'\"\s#]+\.ya?ml)(?:#/components/(?P<section>[A-Za-z]+)/(?P<schema>[^'\"\s]+))?")


# ================================================================ index des schémas

class Catalog:
    """Versions existantes d'une section (components/<section>) : {Nom: {majeure: [mineures triées]}}."""

    def __init__(self, section_dir: Path, section: str = "schemas"):
        self.dir = section_dir
        self.section = section
        self.versions: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.letter: dict[str, str] = {}  # « V » (actuel) ou « v » (ancien format), par schéma
        for folder in (d for d in self.dir.iterdir() if d.is_dir()) if self.dir.is_dir() else ():
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
        return f"#/components/{self.section}/{name}" if self.letter.get(name, "V") == "V" else ""

    def ref(self, name: str, major: int, minor: int) -> str:
        return self.file_name(name, major, minor) + self.fragment(name)

    def path(self, name: str, major: int, minor: int) -> Path:
        return self.dir / name / self.file_name(name, major, minor)

    def content(self, name: str, major: int, minor: int) -> dict:
        """Le schéma d'une version : components.schemas.<Nom> (format actuel) ou le fichier entier (ancien)."""
        key = (name, major, minor)
        if key not in self._files:
            doc = load(self.path(name, major, minor))
            schemas = (doc.get("components") or {}).get(self.section) or {} if isinstance(doc, dict) else {}
            self._files[key] = schemas.get(name, doc)
        return self._files[key]

    def files(self) -> list[Path]:
        return [f for d in self.dir.iterdir() if d.is_dir() for f in d.glob("*.yaml")] if self.dir.is_dir() else []

    def label(self, name: str) -> str:
        """Nom affiché : section/Nom, sauf pour les schémas."""
        return name if self.section == "schemas" else f"{self.section}/{name}"

    def matching_major(self, name: str, inline: dict, names: set, renamed: dict[str, str]) -> int | None:
        """Majeure de la version existante identique au schéma défini dans une API
        (name : nouveau nom ; renamed : {ancien nom: nouveau nom} des schémas de l'API)."""
        target = shape(to_sibling_refs(
            map_refs(inline, names, lambda dep: f"../{renamed[dep]}/{self.ref(renamed[dep], 0, 0)}")))
        for major, minor in reversed(self.all(name)):
            if shape(self.content(name, major, minor)) == target:
                return major
        return None


def load_catalogs(components_dir: Path) -> dict[str, Catalog]:
    """Un Catalog par section de components/ (schemas obligatoire)."""
    if not (components_dir / "schemas").is_dir():
        raise ValueError(f"Dossier des schémas introuvable : {(components_dir / 'schemas').resolve()} "
                         f"(lancez d'abord split_openapi.py, ou adaptez COMPONENTS_DIR dans openapi_common.py)")
    return {section: Catalog(components_dir / section, section) for section in SECTIONS}


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
    # Un schéma renommé à l'extraction porte le nom de l'API : Error -> ErrorIgdRdvBulkCallback.
    api = api_name(label)
    alternative = {name: renamed_for_api(renamed[name], schema_naming(name)[1], api)[0] for name in used}
    majors: dict[str, int] = {}
    for _ in range(len(used) + 1):  # renommer une dépendance change le contenu attendu des autres
        majors, changed = {}, False
        for name in sorted(used & names):
            for candidate in dict.fromkeys((renamed[name], alternative[name])):
                if catalog.all(candidate) and (
                        major := catalog.matching_major(candidate, inline[name], names, renamed)) is not None:
                    majors[name] = major
                    changed |= candidate != renamed[name]
                    renamed[name] = candidate
                    break
        if not changed:
            break
    errors = []
    for name in sorted(used - set(majors)):
        new = renamed[name]
        if name not in inline:
            errors.append(f"{label} : #/components/schemas/{name} référencé mais non défini dans l'API")
        elif not catalog.all(new) and not catalog.all(alternative[name]):
            errors.append(f"{label} : schéma {name} ({new}) absent de schemas/ (lancez split_openapi.py)")
        else:
            errors.append(f"{label} : {name} défini dans l'API ne correspond à aucune version de "
                          f"schemas/{new}/ (modifié ? lancez extract_api_schemas.py)")
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


def resolve_block_refs(doc: dict, catalogs: dict[str, Catalog], api_dir: Path,
                       naming=disk_name) -> tuple[dict, list[str]]:
    """Étape 0 : $ref vers le fichier commun en bloc (…/rdv-actor-schemas.yaml#/components/<section>/X)
    -> fichier découpé correspondant (components/<section>/X/X_V<dernière>.yaml). Une cible inconnue reste telle quelle."""
    changes = []

    def convert(value: str) -> str:
        section, original = block_ref(value)
        catalog = catalogs.get(section)
        try:
            name = naming(section, original)
        except ValueError:
            return value
        if catalog is None or not catalog.all(name):
            return value
        major, minor = catalog.all(name)[-1]
        changes.append(f"{original} (fichier commun) -> {catalog.label(name)} v{major}.{minor}")
        prefix = os.path.relpath(catalog.dir, api_dir).replace(os.sep, "/")
        return f"{prefix}/{name}/{catalog.ref(name, major, minor)}"

    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        return {k: (convert(v) if k == "$ref" and block_ref(v) else walk(v)) for k, v in node.items()}

    return walk(doc), sorted(set(changes))


def upgrade_versions(text: str, catalogs: dict[str, Catalog]) -> tuple[str, list[str]]:
    """Étape 2 : chaque <section>/X/X_va.b.yaml -> dernière version a.y existante."""
    changes = []

    def upgrade(m: re.Match) -> str:
        name, major, minor = m["name"], int(m["major"]), int(m["minor"])
        catalog = catalogs.get(m["section"]) or catalogs["schemas"]
        newest = catalog.latest(name, major)
        if newest is None or newest <= minor:
            return m[0]
        changes.append(f"{catalog.label(name)} : v{major}.{minor} -> v{major}.{newest}")
        section = f"{m['section']}/" if m["section"] else ""
        return f"{section}{name}/{name}_{m['v']}{major}.{newest}.yaml"

    return VERSIONED_REF.sub(upgrade, text), changes


def sync_api(api_file: Path, catalogs: dict[str, Catalog], write: bool, root: Path,
             naming=disk_name) -> tuple[list[str], list[str]]:
    """Synchronise une API. Renvoie (changements, erreurs) ; rien n'est écrit en cas d'erreur."""
    label = display(api_file, root)
    text = api_file.read_text(encoding="utf-8")
    changes: list[str] = []

    try:
        doc = loads(text) if LOCAL in text or "components" in text else None
    except yaml.YAMLError as error:  # une API invalide est signalée, les autres sont quand même traitées
        return [], [str(yaml_error(label, error))]
    catalog = catalogs["schemas"]
    prefix = os.path.relpath(catalog.dir, api_file.parent).replace(os.sep, "/")
    if doc is not None and ".yaml#/components/" in text:
        doc, changes = resolve_block_refs(doc, catalogs, api_file.parent, naming)
    has_inline = bool(((doc or {}).get("components") or {}).get("schemas"))
    if doc is not None and (has_inline or LOCAL in text):
        new_doc, changed, errors = externalize(doc, catalog, prefix, label)
        if errors:
            return [], errors
        changes += changed
        doc = new_doc
    if changes:
        text = dump(doc)

    text, upgraded = upgrade_versions(text, catalogs)
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

def discover_apis(apis_dir: Path, components_dir: Path) -> tuple[list[Path], list[Path]]:
    """API de <dossier des API>/<api>/v<x>/<api>.yaml : (dernière version de chaque API, anciennes versions).
    Seule la dernière version est traitée : les anciennes restent figées."""
    if not apis_dir.is_dir():
        raise ValueError(f"Dossier des API introuvable : {apis_dir.resolve()} (adaptez APIS_DIR dans openapi_common.py)")
    latest, older = [], []
    for api in sorted(p for p in apis_dir.iterdir() if p.is_dir() and p.resolve() != components_dir.resolve()):
        versions = sorted((int(d.name[1:]), d / f"{api.name}.yaml") for d in api.iterdir()
                          if d.is_dir() and re.fullmatch(r"v\d+", d.name) and (d / f"{api.name}.yaml").exists())
        if versions:
            latest.append(versions[-1][1])
            older += [file for _, file in versions[:-1]]
    return latest, older


def broken_refs(files: list[Path], root: Path) -> list[str]:
    """Références relatives ($ref, discriminator.mapping) vers un fichier absent, ou vers un
    schéma absent de components.schemas du fichier visé."""
    errors = []
    schemas_in: dict[tuple[Path, str], set] = {}
    for file in files:
        for m in RELATIVE_PATH.finditer(file.read_text(encoding="utf-8")):
            target = (file.parent / m["ref"]).resolve()
            if not target.exists():
                errors.append(f"{display(file, root)} : $ref introuvable -> {m['ref']}")
                continue
            if m["schema"]:
                key = (target, m["section"])
                if key not in schemas_in:
                    try:
                        doc = load(target)
                    except ValueError as error:
                        errors.append(str(error))
                        schemas_in[key] = None
                        continue
                    schemas_in[key] = set(((doc or {}).get("components") or {}).get(m["section"]) or {})
                if schemas_in[key] is None:
                    continue
                if m["schema"] not in schemas_in[key]:
                    errors.append(f"{display(file, root)} : {m['section']}.{m['schema']} absent de {m['ref']}")
    return list(dict.fromkeys(errors))


def run(components_dir: Path, apis_dir: Path, apis: list[Path], mode: str,
        catalogs: dict[str, Catalog] | None = None, naming=disk_name,
        title: str = "dernière version de chaque API") -> int:
    """mode : write | dry-run | check. Renvoie le code de sortie.
    Sans API donnée : dernière version de chaque API de apis_dir.
    catalogs, naming : fournis par la v2 (un seul dossier schemas/ pour toutes les sections)."""
    catalogs = catalogs or load_catalogs(components_dir)
    unique = list({id(c): c for c in catalogs.values()}.values())
    targets, older = (apis, []) if apis else discover_apis(apis_dir, components_dir)
    root = apis_dir
    tag = {"dry-run": "[simulation] ", "check": "[contrôle] "}.get(mode, "")

    # Une ligne par API : à jour, mise à jour, ou erreur.
    results = []
    for api in targets:
        api_changes, api_errors = sync_api(api, catalogs, mode == "write", root, naming)
        results.append((display(api, root), api_changes, api_errors))
    updated = sum(1 for _, changes, errors in results if changes and not errors)
    failed = sum(1 for _, _, errors in results if errors)
    print(f"{tag}Synchronisation de {len(targets)} API ({title}) :")
    for label, changes, errors in results:
        if errors:
            print(f"  ✖ {label} : {len(errors)} erreur(s), API non modifiée")
            for error in dict.fromkeys(errors):
                print(f"      ✖ {error.split(' : ', 1)[1] if error.startswith(label + ' : ') else error}")
        elif changes:
            verb = "à mettre à jour" if mode == "check" else "mise à jour" if mode == "write" else "serait mise à jour"
            print(f"  ↑ {label} : {verb}")
            for change in dict.fromkeys(changes):
                print(f"      ↑ {change}")
        else:
            print(f"  ✔ {label} : à jour")
    if older:
        print(f"  · anciennes versions ignorées ({len(older)}) : "
              + ", ".join(display(file.parent, root) for file in older))

    errors = []
    if mode in ("write", "check"):
        errors = broken_refs(targets + [f for c in unique for f in c.files()], root)
        if errors:
            print(f"{tag}✖ Références cassées :")
            for error in errors:
                print(f"  ✖ {error}")
    changelogs = {catalog.label(name): added for catalog in unique
                  for name in sorted(catalog.versions)
                  if (added := update_changelog(catalog, name, write=mode == "write"))}
    if changelogs:
        print(f"{tag}{'Changelog à compléter' if mode == 'check' else 'Changelog mis à jour'} :")
        for name, added in changelogs.items():
            print(f"  {name}/{CHANGELOG} : + {', '.join(added)}")

    print(f"{tag}Bilan : {len(targets) - updated - failed} à jour, {updated} "
          f"{'à mettre à jour' if mode == 'check' else 'mise(s) à jour'}, {failed} en erreur"
          + (f", {len(errors)} référence(s) cassée(s)" if errors else ""))
    return 1 if failed or errors or (mode == "check" and (updated or changelogs)) else 0


# ================================================================ mode --watch

def snapshot(*folders: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for folder in folders for p in folder.rglob("*.yaml")}


def watch(components_dir: Path, apis_dir: Path, apis: list[Path], interval: float) -> None:
    print(f"Surveillance de {components_dir} et {apis_dir} (Ctrl+C pour arrêter)…")
    run(components_dir, apis_dir, apis, "write")
    before = snapshot(components_dir, apis_dir)
    try:
        while True:
            time.sleep(interval)
            if snapshot(components_dir, apis_dir) != before:
                print(f"\n[{time.strftime('%H:%M:%S')}] modification détectée")
                try:
                    run(components_dir, apis_dir, apis, "write")
                except (ValueError, OSError, yaml.YAMLError) as error:
                    print(f"✖ {error}", file=sys.stderr)
                before = snapshot(components_dir, apis_dir)  # inclut nos propres écritures
    except KeyboardInterrupt:
        print("\nArrêt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise les $ref des API avec les schémas versionnés.")
    parser.add_argument("apis", type=Path, nargs="*",
                        help="API à traiter (défaut : toutes les <dossier des API>/<api>/v<x>/<api>.yaml)")
    parser.add_argument("--components-dir", type=Path, default=COMPONENTS_DIR, help=f"défaut : {COMPONENTS_DIR}")
    parser.add_argument("--apis-dir", type=Path, default=APIS_DIR, help=f"défaut : {APIS_DIR}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="affiche sans écrire")
    mode.add_argument("--check", action="store_true", help="code 1 si quelque chose n'est pas à jour (CI)")
    mode.add_argument("--watch", nargs="?", type=float, const=2.0, metavar="SECONDES",
                      help="synchronise automatiquement à chaque modification")
    args = parser.parse_args()
    try:
        if args.watch is not None:
            watch(args.components_dir, args.apis_dir, args.apis, args.watch)
        else:
            mode = "dry-run" if args.dry_run else "check" if args.check else "write"
            sys.exit(run(args.components_dir, args.apis_dir, args.apis, mode))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
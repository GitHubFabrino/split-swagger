#!/usr/bin/env python3
"""
Synchronise les $ref des API avec les schémas versionnés.

    <dossier>/schemas/<Nom>/<Nom>_v<x>.<y>.yaml     schémas (produits par split_openapi.py)
    <dossier>/<api>/v<x>/<api>.yaml                 API (trouvées automatiquement)

Pour chaque API :
  1. $ref locales "#/components/schemas/Order" -> "../../schemas/Order/Order_v<x.y>.yaml"
     Le schéma défini dans l'API est comparé aux versions existantes pour trouver sa majeure,
     puis retiré du fichier (il vit désormais dans schemas/). S'il ne correspond à aucune
     version existante, l'API n'est pas modifiée : lancez d'abord split_openapi.py.
  2. $ref versionnées "schemas/Order/Order_v1.0.yaml" -> dernière version 1.y existante.
     Jamais vers une autre majeure (changement cassant).
Les fichiers de schémas ne sont jamais modifiés. Les extensions x-* sont laissées telles quelles.

Usage :
    python3 sync_schemas.py <dossier> [api.yaml ...] [--dry-run | --check | --watch [SECONDES]]

    api.yaml ...   API à traiter (défaut : toutes les <dossier>/<api>/v<x>/<api>.yaml)
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
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import LOCAL, dump, load, loads, map_refs, schema_refs, shape  # noqa: E402

SCHEMA_FILE = re.compile(r"^(?P<name>.+)_v(?P<major>\d+)\.(?P<minor>\d+)\.yaml$")
VERSIONED_REF = re.compile(
    r"(?P<prefix>schemas/)(?P<name>[^/\s'\"#]+)/(?P=name)_v(?P<major>\d+)\.(?P<minor>\d+)\.yaml")
# Toute valeur qui est un chemin relatif vers un .yaml : $ref et discriminator.mapping
RELATIVE_PATH = re.compile(r":\s*['\"]?(?P<ref>\.\.?/[^'\"\s#]+\.ya?ml)")


# ================================================================ index des schémas

class Catalog:
    """Versions existantes dans <dossier>/schemas/ : {Nom: {majeure: [mineures triées]}}."""

    def __init__(self, root: Path):
        self.dir = root / "schemas"
        if not self.dir.is_dir():
            raise ValueError(f"Dossier introuvable : {self.dir} (lancez d'abord split_openapi.py)")
        self.versions: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        for folder in (d for d in self.dir.iterdir() if d.is_dir()):
            for file in folder.iterdir():
                m = SCHEMA_FILE.match(file.name)
                if m and m["name"] == folder.name:
                    self.versions[folder.name][int(m["major"])].append(int(m["minor"]))
        for majors in self.versions.values():
            for minors in majors.values():
                minors.sort()
        self._files: dict[tuple[str, int, int], dict] = {}

    def latest(self, name: str, major: int) -> int | None:
        minors = self.versions.get(name, {}).get(major)
        return minors[-1] if minors else None

    def all(self, name: str) -> list[tuple[int, int]]:
        return sorted((major, minor) for major, minors in self.versions.get(name, {}).items() for minor in minors)

    def content(self, name: str, major: int, minor: int) -> dict:
        key = (name, major, minor)
        if key not in self._files:
            self._files[key] = load(self.dir / name / f"{name}_v{major}.{minor}.yaml")
        return self._files[key]

    def files(self) -> list[Path]:
        return [f for d in self.dir.iterdir() if d.is_dir() for f in d.glob("*.yaml")]

    def matching_major(self, name: str, inline: dict, names: set) -> int | None:
        """Majeure de la version existante identique au schéma défini dans une API."""
        target = shape(map_refs(inline, names, lambda dep: f"../{dep}/{dep}_v0.0.yaml"))
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
    """Étape 1 : remplace #/components/schemas/X par le fichier versionné et retire les schémas
    définis dans l'API. Renvoie (document modifié ou None si erreur, changements, erreurs)."""
    inline = (doc.get("components") or {}).get("schemas") or {}
    names = set(inline)
    majors: dict[str, int] = {}
    errors = []
    for name in sorted(schema_refs(doc, names) | names):
        if not catalog.all(name):
            errors.append(f"{label} : schéma {name} absent de schemas/ (lancez split_openapi.py)")
        elif name not in inline:
            errors.append(f"{label} : #/components/schemas/{name} référencé mais non défini dans l'API")
        elif (major := catalog.matching_major(name, inline[name], names)) is None:
            errors.append(f"{label} : {name} défini dans l'API ne correspond à aucune version de "
                          f"schemas/{name}/ (modifié ? lancez split_openapi.py sur le nouveau fichier)")
        else:
            majors[name] = major
    if errors:
        return None, [], errors

    def to_file(name: str) -> str:
        major = majors[name]
        return f"{prefix}/{name}/{name}_v{major}.{catalog.latest(name, major)}.yaml"

    components = {k: v for k, v in (doc.get("components") or {}).items() if k != "schemas"}
    new_doc = map_refs({**doc, "components": components}, names, to_file)
    if not new_doc["components"]:
        del new_doc["components"]
    changes = [f"{name} : #/components/schemas -> v{majors[name]}.{catalog.latest(name, majors[name])}"
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
        return f"{m['prefix']}{name}/{name}_v{major}.{newest}.yaml"

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


# ================================================================ découverte et contrôles

def discover_apis(root: Path) -> list[Path]:
    """Toutes les <dossier>/<api>/v<x>/<api>.yaml."""
    found = []
    for api in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "schemas"):
        for version_dir in sorted(p for p in api.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)):
            file = version_dir / f"{api.name}.yaml"
            if file.exists():
                found.append(file)
    return found


def broken_refs(files: list[Path], root: Path) -> list[str]:
    """Chemins relatifs ($ref, discriminator.mapping) qui mènent à un fichier absent."""
    errors = []
    for file in files:
        for m in RELATIVE_PATH.finditer(file.read_text(encoding="utf-8")):
            if not (file.parent / m["ref"]).resolve().exists():
                errors.append(f"{display(file, root)} : $ref introuvable -> {m['ref']}")
    return list(dict.fromkeys(errors))


def run(root: Path, apis: list[Path], mode: str) -> int:
    """mode : write | dry-run | check. Renvoie le code de sortie."""
    catalog = Catalog(root)
    targets = apis or discover_apis(root)
    changes: dict[str, list[str]] = {}
    errors: list[str] = []
    for api in targets:
        api_changes, api_errors = sync_api(api, catalog, mode == "write", root)
        if api_changes:
            changes[display(api, root)] = api_changes
        errors += api_errors
    if mode in ("write", "check"):
        errors += broken_refs(targets + catalog.files(), root)

    tag = {"dry-run": "[simulation] ", "check": "[contrôle] "}.get(mode, "")
    if changes:
        print(f"{tag}{'Mises à jour nécessaires' if mode == 'check' else 'Synchronisé'} ({len(targets)} API) :")
        for api, items in changes.items():
            print(f"  {api}")
            for item in dict.fromkeys(items):
                print(f"    ↑ {item}")
    elif not errors:
        print(f"{tag}✔ {len(targets)} API à jour avec les dernières versions des schémas")
    if errors:
        print(f"{tag}✖ Erreurs :", file=sys.stderr)
        for error in dict.fromkeys(errors):
            print(f"  ✖ {error}", file=sys.stderr)
    return 1 if errors or (mode == "check" and changes) else 0


# ================================================================ mode --watch

def snapshot(root: Path) -> dict[str, float]:
    return {str(p): p.stat().st_mtime for p in root.rglob("*.yaml")}


def watch(root: Path, apis: list[Path], interval: float) -> None:
    print(f"Surveillance de {root} (Ctrl+C pour arrêter)…")
    run(root, apis, "write")
    before = snapshot(root)
    try:
        while True:
            time.sleep(interval)
            if snapshot(root) != before:
                print(f"\n[{time.strftime('%H:%M:%S')}] modification détectée")
                try:
                    run(root, apis, "write")
                except (ValueError, OSError, yaml.YAMLError) as error:
                    print(f"✖ {error}", file=sys.stderr)
                before = snapshot(root)  # inclut nos propres écritures
    except KeyboardInterrupt:
        print("\nArrêt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise les $ref des API avec les schémas versionnés.")
    parser.add_argument("root", type=Path, help="dossier contenant schemas/")
    parser.add_argument("apis", type=Path, nargs="*", help="API à traiter (défaut : <dossier>/<api>/v<x>/<api>.yaml)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="affiche sans écrire")
    mode.add_argument("--check", action="store_true", help="code 1 si quelque chose n'est pas à jour (CI)")
    mode.add_argument("--watch", nargs="?", type=float, const=2.0, metavar="SECONDES",
                      help="synchronise automatiquement à chaque modification")
    args = parser.parse_args()
    try:
        if args.watch is not None:
            watch(args.root, args.apis, args.watch)
        else:
            sys.exit(run(args.root, args.apis, "dry-run" if args.dry_run else "check" if args.check else "write"))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

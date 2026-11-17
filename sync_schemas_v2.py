#!/usr/bin/env python3
"""
Synchronisation v2 : synchronise UNE API choisie (ou quelques-unes, listées) avec le dossier unique
des schémas produit par split_openapi_v2.py (<schemas>/<Nom>/<Nom>_V<x.y>.yaml, toutes sections).

Mêmes étapes que sync_schemas.py, sur l'API choisie seulement :
  0. $ref vers le fichier commun en bloc (…/rdv-actor-schemas.yaml#/components/<section>/X)
     -> fichier découpé (QueryX, ResponseX, SecurityX, ou le schéma renommé), dernière version ;
  1. schémas définis dans l'API identiques à une version existante -> $ref vers cette version ;
  2. chaque $ref vers un fichier découpé -> dernière version de la même majeure ;
  3. contrôle des références cassées, CHANGELOG.md des composants.

Une ancienne version d'API (rdv-actor/v1 quand v3 existe) est refusée, sauf avec --allow-old.

Usage :
    python3 sync_schemas_v2.py <api.yaml> [api.yaml ...] [--schemas-dir D] [--apis-dir D]
                               [--dry-run | --check | --watch [S]] [--allow-old]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import APIS_DIR, SCHEMAS_DIR_V2, SECTIONS, load, naming_v2  # noqa: E402
from sync_schemas import Catalog, discover_apis, display, run, snapshot  # noqa: E402


class FlatCatalog(Catalog):
    """Catalog d'un dossier unique : la section de chaque composant est lue dans son fichier
    (components.<section>.<Nom>)."""

    def __init__(self, schemas_dir: Path):
        if not schemas_dir.is_dir():
            raise ValueError(f"Dossier des schémas introuvable : {schemas_dir.resolve()} "
                             f"(lancez d'abord split_openapi_v2.py, ou adaptez SCHEMAS_DIR_V2 dans openapi_common.py)")
        super().__init__(schemas_dir, "schemas")
        self._sections: dict[str, str] = {}

    def section_of(self, name: str) -> str:
        if name not in self._sections:
            section = "schemas"
            versions = self.all(name)
            if versions:
                doc = load(self.path(name, *versions[-1]))
                components = (doc.get("components") or {}) if isinstance(doc, dict) else {}
                section = next((s for s in SECTIONS if name in (components.get(s) or {})), "schemas")
            self._sections[name] = section
        return self._sections[name]

    def fragment(self, name: str) -> str:
        return f"#/components/{self.section_of(name)}/{name}" if self.letter.get(name, "V") == "V" else ""

    def content(self, name: str, major: int, minor: int) -> dict:
        key = (name, major, minor)
        if key not in self._files:
            doc = load(self.path(name, major, minor))
            items = (doc.get("components") or {}).get(self.section_of(name)) or {} if isinstance(doc, dict) else {}
            self._files[key] = items.get(name, doc)
        return self._files[key]

    def label(self, name: str) -> str:
        return name


def flat_catalogs(schemas_dir: Path) -> dict[str, Catalog]:
    """Le même catalogue pour toutes les sections (un seul dossier)."""
    catalog = FlatCatalog(schemas_dir)
    return {section: catalog for section in SECTIONS}


def check_targets(apis: list[Path], apis_dir: Path, schemas_dir: Path, allow_old: bool) -> None:
    """Vérifie que chaque API existe et, sauf --allow-old, qu'elle est la dernière version de son API."""
    missing = [str(api) for api in apis if not api.is_file()]
    if missing:
        raise ValueError(f"API introuvable : {', '.join(missing)}")
    if allow_old:
        return
    _, older = discover_apis(apis_dir, schemas_dir)
    old = {file.resolve() for file in older}
    refused = [display(api, apis_dir) for api in apis if api.resolve() in old]
    if refused:
        raise ValueError(f"ancienne version d'API, non modifiée : {', '.join(refused)} "
                         f"(une version plus récente existe ; relancez avec --allow-old pour forcer)")


def sync_v2(apis: list[Path], schemas_dir: Path, apis_dir: Path, mode: str) -> int:
    return run(schemas_dir, apis_dir, apis, mode, catalogs=flat_catalogs(schemas_dir), naming=naming_v2,
               title="API choisie" if len(apis) == 1 else "API choisies")


def watch(apis: list[Path], schemas_dir: Path, apis_dir: Path, interval: float) -> None:
    print(f"Surveillance de {schemas_dir}/ et de {len(apis)} API (Ctrl+C pour arrêter)")
    sync_v2(apis, schemas_dir, apis_dir, "write")
    state = snapshot(schemas_dir, *(api.parent for api in apis))
    try:
        while True:
            time.sleep(interval)
            current = snapshot(schemas_dir, *(api.parent for api in apis))
            if current != state:
                print()
                sync_v2(apis, schemas_dir, apis_dir, "write")
                state = snapshot(schemas_dir, *(api.parent for api in apis))
    except KeyboardInterrupt:
        print("\nSurveillance arrêtée.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronise une API choisie avec le dossier unique des schémas (v2).")
    parser.add_argument("apis", type=Path, nargs="+", help="API à synchroniser (ex. swaggers/RDV/rdv-actor/v3/rdv-actor.yaml)")
    parser.add_argument("--schemas-dir", type=Path, default=SCHEMAS_DIR_V2, help=f"défaut : {SCHEMAS_DIR_V2}")
    parser.add_argument("--apis-dir", type=Path, default=APIS_DIR, help=f"défaut : {APIS_DIR}")
    parser.add_argument("--allow-old", action="store_true", help="accepte une ancienne version d'API")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="affiche les changements sans rien écrire")
    group.add_argument("--check", action="store_true", help="code 1 si l'API n'est pas à jour ou a un $ref cassé (CI)")
    group.add_argument("--watch", type=float, nargs="?", const=2.0, metavar="S",
                       help="resynchronise à chaque modification (toutes les S secondes, défaut 2)")
    args = parser.parse_args()
    try:
        check_targets(args.apis, args.apis_dir, args.schemas_dir, args.allow_old)
        if args.watch:
            watch(args.apis, args.schemas_dir, args.apis_dir, args.watch)
            return
        mode = "check" if args.check else "dry-run" if args.dry_run else "write"
        sys.exit(sync_v2(args.apis, args.schemas_dir, args.apis_dir, mode))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
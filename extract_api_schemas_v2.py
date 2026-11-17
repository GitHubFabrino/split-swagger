#!/usr/bin/env python3
"""
Extraction v2 : extrait les composants d'UNE API choisie (ou de quelques-unes, listées) vers le dossier
unique des schémas, les retire de l'API, puis synchronise l'API.

Pour chaque API :
  1. schemas, parameters, responses et securitySchemes de components sont découpés dans
     <schemas>/<Nom>/<Nom>_V<x.y>.yaml, comme split_openapi_v2.py (même renommage Query… / Response… /
     Security…, même format, même versionnement), avec la vérification détaillée quand le nom existe déjà :
       - identique                                   -> réutilisé ;
       - différent, existant créé depuis une autre source -> renommé avec le nom de l'API
                                                        (ResponseBadRequest -> ResponseBadRequestRdvAdminActor) ;
       - différent, existant créé depuis la même API   -> nouvelle version.
     Un changement cassant sans --major bloque l'API : rien n'est écrit.
  2. l'API est réécrite : les composants extraits sont retirés de components et chaque usage
     (#/components/parameters/actorIdParam …) pointe vers le fichier extrait.
     securitySchemes : l'entrée reste (la section security: désigne le scheme par son nom),
     sa valeur devient une $ref vers le fichier extrait.
  3. l'API est synchronisée (comme sync_schemas_v2.py).

Les parameters écrits directement dans une opération (hors components) ne sont pas touchés.
Une ancienne version d'API est refusée, sauf avec --allow-old.

Usage :
    python3 extract_api_schemas_v2.py <api.yaml> [api.yaml ...] [--schemas-dir D] [--apis-dir D]
                                      [--major Nom,Nom] [--dry-run] [--allow-old]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import (  # noqa: E402
    APIS_DIR, SCHEMA_FILE_REF, SCHEMAS_DIR_V2, SECTIONS, api_name, dump, load, local_prefix, map_refs, naming_v2, rename_map)
from extract_api_schemas import checking_splitter  # noqa: E402
from split_openapi import file_name, fragment  # noqa: E402
from split_openapi_v2 import split_components_v2  # noqa: E402
from sync_schemas import display, resolve_block_refs  # noqa: E402
from sync_schemas_v2 import check_targets, flat_catalogs, sync_v2  # noqa: E402


def _already_extracted(value) -> bool:
    return (isinstance(value, dict) and set(value) == {"$ref"} and isinstance(value["$ref"], str)
            and bool(SCHEMA_FILE_REF.search(value["$ref"])))


def names_in(components: dict) -> set[str]:
    """Noms acceptés par --major pour cette API : noms d'origine et noms v2."""
    found = set()
    for section in SECTIONS:
        items = components.get(section) or {}
        found |= set(items)
        found |= set(rename_map(items).values()) if section == "schemas" else {naming_v2(section, n) for n in items}
    return found


def rewrite_api(doc: dict, extracted: dict[str, dict[str, tuple]], rel: str) -> dict:
    """Retire de components les composants extraits et fait pointer chaque usage vers leur fichier."""
    def target(section: str, final: str, version) -> str:
        return f"{rel}/{final}/{file_name(final, version)}{fragment(final, section)}"

    components = dict(doc.get("components") or {})
    for section in extracted:
        rest = {name: value for name, value in (components.get(section) or {}).items()
                if name not in extracted[section]}
        if rest and section != "securitySchemes":
            components[section] = rest
        else:
            components.pop(section, None)
    new = {**doc, "components": components}
    for section, mapping in extracted.items():
        new = map_refs(new, set(mapping), lambda name, s=section, m=mapping: target(s, *m[name])
                       if name in m else f"{local_prefix(s)}{name}", prefix=local_prefix(section))
    if "securitySchemes" in extracted:  # gardés par leur nom, valeur -> fichier extrait
        before = (doc.get("components") or {}).get("securitySchemes") or {}
        new["components"]["securitySchemes"] = {
            name: ({"$ref": target("securitySchemes", *extracted["securitySchemes"][name])}
                   if name in extracted["securitySchemes"] else value)
            for name, value in before.items()}
    if not new["components"]:
        del new["components"]
    return new


def extract_one(api_file: Path, schemas_dir: Path, apis_dir: Path, forced: set[str], dry_run: bool) -> tuple[bool, set]:
    """Extrait une API. Renvoie (quelque chose extrait, noms --major utilisés). ValueError si bloquée."""
    label = display(api_file, apis_dir)
    doc = load(api_file)
    if not isinstance(doc, dict):
        raise ValueError("fichier YAML OpenAPI attendu")
    # Les références au fichier commun en bloc pointent d'abord vers les fichiers découpés.
    doc, _ = resolve_block_refs(doc, flat_catalogs(schemas_dir), api_file.parent, naming_v2)
    components = doc.get("components") or {}
    # Une entrée qui n'est qu'une $ref vers un fichier déjà découpé (securityScheme gardé par nom) est déjà extraite.
    present = {section: items for section in SECTIONS
               if (items := {name: value for name, value in (components.get(section) or {}).items()
                             if not _already_extracted(value)})}
    if not present:
        print(f"  ✔ {label} : aucun composant défini dans l'API (déjà extrait)")
        return False, set()

    used = {name for name in forced if name in names_in(present)}
    api = api_name(label)
    source_ref = os.path.relpath(api_file.resolve(), (schemas_dir / "X").resolve()).replace(os.sep, "/")

    def run_split(simulate: bool, report: dict):
        return split_components_v2(present, schemas_dir, used, simulate, label, label, source_ref, strict=True,
                                   splitter_class_for=lambda section, origins: checking_splitter(api, origins),
                                   report=report)

    # 1. Contrôle complet (sans rien écrire) : un blocage sur une section n'écrit rien des autres.
    with contextlib.redirect_stdout(io.StringIO()):
        run_split(True, {})
    count = sum(len(items) for items in present.values())
    print(f"  → {label} : {count} composant(s) défini(s) dans l'API "
          f"({', '.join(f'{s} {len(i)}' for s, i in present.items())})")
    report: dict = {}
    run_split(dry_run, report)

    # 2. Réécriture de l'API.
    rel = os.path.relpath(schemas_dir.resolve(), api_file.parent.resolve()).replace(os.sep, "/")
    new_doc = rewrite_api(doc, report, rel)
    removed = sum(len(m) for s, m in report.items() if s != "securitySchemes")
    kept = len(report.get("securitySchemes", {}))
    print(f"  {'[simulation] ' if dry_run else ''}✂ {label} : {removed} composant(s) retiré(s) de l'API"
          + (f", {kept} securityScheme(s) gardé(s) par nom avec $ref" if kept else ""))
    if not dry_run:
        api_file.write_text(dump(new_doc), encoding="utf-8")
    return True, used


def extract_v2(apis: list[Path], schemas_dir: Path, apis_dir: Path, forced: set[str], dry_run: bool) -> int:
    tag = "[simulation] " if dry_run else ""
    print(f"{tag}Extraction de {len(apis)} API choisie(s) -> {schemas_dir}/")
    done, failed, used = [], [], set()
    for api_file in apis:
        label = display(api_file, apis_dir)
        try:
            extracted, names = extract_one(api_file, schemas_dir, apis_dir, forced, dry_run)
            used |= names
            done.append(api_file)
            print()
        except ValueError as error:
            print(f"  ✖ {label} : {error}")
            failed.append(label)
    unused = sorted(forced - used)
    if unused:
        print(f"  ⚠ --major : composant(s) trouvé(s) dans aucune API choisie : {', '.join(unused)}")
    print(f"{tag}Bilan de l'extraction : {len(done)} API traitée(s), {len(failed)} en erreur")
    if dry_run:
        print("[simulation] Synchronisation non simulée : relancez sans --dry-run pour extraire puis synchroniser.")
        return 1 if failed else 0
    if not done:
        return 1
    print()
    code = sync_v2(done, schemas_dir, apis_dir, "write")
    return 1 if failed or code else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrait les composants d'une API choisie vers le dossier unique des schémas, puis la synchronise (v2).")
    parser.add_argument("apis", type=Path, nargs="+", help="API à extraire (ex. swaggers/RDV/rdv-admin-actor/v1/rdv-admin-actor.yaml)")
    parser.add_argument("--schemas-dir", type=Path, default=SCHEMAS_DIR_V2, help=f"défaut : {SCHEMAS_DIR_V2}")
    parser.add_argument("--apis-dir", type=Path, default=APIS_DIR, help=f"défaut : {APIS_DIR}")
    parser.add_argument("--major", default="", help="composants ayant un changement cassant, séparés par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="affiche ce qui serait extrait sans rien écrire")
    parser.add_argument("--allow-old", action="store_true", help="accepte une ancienne version d'API")
    args = parser.parse_args()
    try:
        check_targets(args.apis, args.apis_dir, args.schemas_dir, args.allow_old)
        args.schemas_dir.mkdir(parents=True, exist_ok=True)
        sys.exit(extract_v2(args.apis, args.schemas_dir, args.apis_dir,
                            {name for name in args.major.split(",") if name}, args.dry_run))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
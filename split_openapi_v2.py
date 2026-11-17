#!/usr/bin/env python3
"""
Découpage v2 : toutes les sections du fichier de composants en bloc dans UN SEUL dossier,
un sous-dossier par composant et par nom :

    <schemas>/ErrorOutput/ErrorOutput_V1.0.yaml                 (schéma)
    <schemas>/QueryActorIdParam/QueryActorIdParam_V1.0.yaml     (parameter)
    <schemas>/ResponseBadRequest/ResponseBadRequest_V1.0.yaml   (response)
    <schemas>/SecurityOauth2/SecurityOauth2_V1.0.yaml           (securityScheme)

Renommage (openapi_common.naming_v2) :
    schemas         règles v1 (E_Error_Output -> ErrorOutput, S_Address -> AddressObject …)
    parameters      Query + nom en PascalCase    actorIdParam -> QueryActorIdParam
    responses       Response + nom               BadRequest   -> ResponseBadRequest
    securitySchemes Security + nom               oauth2       -> SecurityOauth2
Deux composants qui donneraient le même nom : erreur.

Le reste est identique à split_openapi.py : en-tête et info, contenu sous components.<section>.<Nom>,
versionnement automatique (réutilisation, mineure, --major, cascade, y compris d'une section à l'autre),
--dry-run. Les références entre composants pointent vers le dossier voisin :
    ../ErrorOutput/ErrorOutput_V1.0.yaml#/components/schemas/ErrorOutput
Les sections non découpées (examples, headers…) restent dans le fichier en bloc ; les références y pointent.

Usage :
    python3 split_openapi_v2.py [composants.yaml] [dossier-schemas] [--major Nom,Nom] [--dry-run]

    Sans argument : SOURCE_FILE et SCHEMAS_DIR_V2 de openapi_common.py.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import (  # noqa: E402
    SCHEMA_FILE_REF, SCHEMAS_DIR_V2, SECTION_KIND, SECTIONS, SOURCE_FILE, local_prefix, map_refs, naming_v2,
    other_refs, rename_map, schema_refs, to_sibling_refs, without_sources)
from split_openapi import (  # noqa: E402
    Version, _keep_in_block, file_name, fragment, prepare_schemas, read_components, split_schemas)

_NOT_SPLIT = re.compile(r"^#/components/(?!(?:" + "|".join(SECTIONS) + r")/)[A-Za-z]+/")


def disk_names_v2(components: dict, where: str) -> dict[str, dict[str, str]]:
    """{section: {nom d'origine: nom sur disque}}. Deux composants -> même nom : erreur."""
    names = {section: (rename_map(components[section]) if section == "schemas"
                       else {name: naming_v2(section, name) for name in components[section]})
             for section in SECTIONS if components.get(section)}
    counts = Counter(disk for mapping in names.values() for disk in mapping.values())
    clashes = sorted(disk for disk, count in counts.items() if count > 1)
    if clashes:
        detail = "; ".join(f"{disk} <- " + ", ".join(f"{section}.{name}" for section, mapping in names.items()
                                                     for name, d in mapping.items() if d == disk)
                           for disk in clashes)
        raise ValueError(f"{where} : plusieurs composants auraient le même nom dans le dossier unique : {detail}")
    return names


def prepare_section_v2(items: dict, section: str, where: str, disk: dict[str, str],
                       done: dict[str, dict[str, tuple[str, Version]]], source_ref: str | None) -> tuple[dict, dict]:
    """Parameters, responses, securitySchemes : renommés (Query…, Response…, Security…).
    Références de la même section : vers le nouveau nom ; vers une section déjà découpée :
    ../<Nom>/<Nom>_V<x.y>.yaml#/components/<section>/<Nom> ; vers une section non découpée : fichier en bloc."""
    names = set(items)
    prepared, missing = {}, []
    for name, item in items.items():
        def same(dep: str, name=name) -> str:
            if dep not in disk:
                missing.append(f"{section}.{name} -> {section}.{dep}")
                return f"{local_prefix(section)}{dep}"
            return f"{local_prefix(section)}{disk[dep]}"
        item = map_refs(item, names, same, prefix=local_prefix(section))
        for other, mapping in done.items():
            def to_file(dep: str, other=other, mapping=mapping, name=name) -> str:
                if dep not in mapping:
                    raise ValueError(f"{where} : {section}.{name} référence {other}.{dep}, absent du fichier")
                target, version = mapping[dep]
                return f"../{target}/{file_name(target, version)}{fragment(target, other)}"
            item = map_refs(item, set(mapping), to_file, prefix=local_prefix(other))
        if source_ref:
            item = _keep_in_block(item, _NOT_SPLIT, source_ref)
        prepared[disk[name]] = to_sibling_refs(without_sources(item))  # refs vers des fichiers déjà découpés
    if missing:
        raise ValueError(f"{where} : référence vers un composant absent du fichier : {missing[0]}")
    new_names = set(prepared)
    external = [ref for ref in other_refs(prepared, new_names, local_prefix(section))
                if not SCHEMA_FILE_REF.search(ref) and not (source_ref and ref.startswith(source_ref + "#"))]
    if external:
        raise ValueError(f"{where} : {section} : {len(external)} référence(s) non prise(s) en charge (ex. {external[0]})")
    for name, item in prepared.items():
        unknown = sorted(schema_refs(item, new_names, local_prefix(section)) - new_names)
        if unknown:
            raise ValueError(f"{where} : {section}.{name} référence un composant absent : {unknown[0]}")
    return prepared, {name: (name, SECTION_KIND[section]) for name in prepared}


def split_components_v2(components: dict, out: Path, forced_major: set[str], dry_run: bool, where: str,
                        source_name: str, source_ref: str | None, strict: bool = False,
                        splitter_class_for=None, report: dict | None = None) -> list[tuple[str, Version, str]]:
    """Découpe les sections présentes de components dans out/ (schémas d'abord).
    splitter_class_for(section, origins) -> classe de Splitter (extraction : vérification détaillée).
    report : rempli avec {section: {nom d'origine: (nom final, version)}}."""
    names = disk_names_v2(components, where)
    known = {key: section for section, mapping in names.items() for pair in mapping.items() for key in pair}
    unknown = sorted(name for name in forced_major if name not in known)
    if unknown:
        raise ValueError(f"--major : composant(s) absent(s) de {where} : {', '.join(unknown)}")

    done: dict[str, dict[str, tuple[str, Version]]] = {}
    bumped: set[str] = set()
    created_all = []
    for section, mapping in names.items():
        items = components[section]
        if section == "schemas":
            prepared, origins = prepare_schemas(items, where)
        else:
            prepared, origins = prepare_section_v2(items, section, where, mapping, done, source_ref)
        forced = {mapping.get(name, name) for name in forced_major if known[name] == section}
        result: dict = {}
        if done:
            print()
        created = split_schemas(prepared, origins, out, forced, dry_run, source_name,
                                f"✔ {where} : {section} ({len(items)}) -> {out}/", strict=strict,
                                splitter_class=splitter_class_for(section, origins) if splitter_class_for else None,
                                section=section, external_bumped=bumped, report=result)
        done[section] = {name: (result["final"][disk], result["chosen"][result["final"][disk]])
                         for name, disk in mapping.items()}
        bumped |= result["bumped"]
        created_all += created
    if report is not None:
        report.update(done)
    return created_all


def split_v2(source: Path, out: Path, forced_major: set[str], dry_run: bool) -> None:
    components = read_components(source)
    source_ref = os.path.relpath(source.resolve(), (out / "X").resolve()).replace(os.sep, "/")
    created = split_components_v2(components, out, forced_major, dry_run, str(source), source.name, source_ref)
    skipped = sorted(set(components) - set(SECTIONS))
    if skipped:
        print(f"  · section(s) non découpée(s), laissée(s) dans {source.name} (les références y pointent) : "
              f"{', '.join(skipped)}")
    if any(not reason.startswith("nouveau") for *_, reason in created):
        print("  → Lancez sync_schemas_v2.py <api> pour repointer une API vers ces nouvelles versions.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Découpe un fichier de composants OpenAPI dans un seul dossier <schemas>/<Nom>/<Nom>_V<x.y>.yaml.")
    parser.add_argument("source", type=Path, nargs="?", default=SOURCE_FILE,
                        help=f"fichier de composants en bloc (défaut : {SOURCE_FILE})")
    parser.add_argument("out", type=Path, nargs="?", default=SCHEMAS_DIR_V2,
                        help=f"dossier des schémas, créé si besoin (défaut : {SCHEMAS_DIR_V2})")
    parser.add_argument("--major", default="", help="composants ayant un changement cassant, séparés par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="affiche ce qui serait créé sans rien écrire")
    args = parser.parse_args()
    try:
        split_v2(args.source, args.out, {name for name in args.major.split(",") if name}, args.dry_run)
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
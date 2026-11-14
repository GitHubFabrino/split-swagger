#!/usr/bin/env python3
"""
Extrait les schémas définis dans les API vers le dossier des schémas commun, puis synchronise ces API.

À lancer après split_openapi.py. Pour la dernière version de chaque API
(<dossier des API>/<api>/v<x>/<api>.yaml, x le plus grand) :

  1. les schémas de components.schemas sont découpés dans <dossier des schémas>/<Nom>/<Nom>_V<x.y>.yaml,
     comme split_openapi.py (même renommage, même format, même versionnement), avec une vérification
     détaillée quand un schéma du même nom existe déjà : comparé propriété par propriété à sa dernière
     version,
       - identique                                  -> réutilisé ;
       - différent, existant créé depuis une autre source (autre API, fichier commun)
                                                    -> renommé avec le nom de l'API :
                                                       Error -> ErrorIgdRdvBulkCallback,
                                                       S_Address -> AddressIgdRdvBulkCallbackObject ;
       - différent, existant créé depuis la même API -> nouvelle version (le schéma évolue).
     Un schéma qui référence un schéma renommé est vérifié à son tour (il peut être renommé aussi).
     Les références vers le fichier commun en bloc (…/rdv-actor-schemas.yaml#/components/schemas/S_X)
     pointent vers le fichier découpé correspondant (XObject_V<x.y>.yaml).
     Un changement cassant d'un schéma de la même API sans --major bloque l'API (rien n'est écrit).
  2. l'API est aussitôt synchronisée (comme sync_schemas.py).

Les anciennes versions d'une API (v1, v2 quand v3 existe) ne sont jamais modifiées.

Usage :
    python3 extract_api_schemas.py [api.yaml ...] [--schemas-dir D] [--apis-dir D] [--major Nom,Nom] [--dry-run]

Dépendances : PyYAML, et openapi_common.py, split_openapi.py, sync_schemas.py à côté de ce script.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import (  # noqa: E402
    APIS_DIR, SCHEMAS_DIR, api_name, load, map_block_refs, renamed_for_api, schema_diff, schema_naming, shape)
from split_openapi import (  # noqa: E402
    Splitter, existing_versions, file_name, fragment, prepare_schemas, schema_file, split_schemas)
from sync_schemas import discover_apis, display, run  # noqa: E402

_SPLIT_FROM = re.compile(r"^# created on: \S+ split from (?P<source>.+)$", re.M)
_DIFF_MARKS = (("added", "+"), ("removed", "-"), ("changed", "~"), ("dependencies", "↻"))


def checking_splitter(api: str, origins: dict) -> type:
    """Splitter qui vérifie, avant de versionner, chaque schéma dont le nom existe déjà."""

    class CheckingSplitter(Splitter):
        def __init__(self, *args):
            super().__init__(*args)
            self.notes: list[str] = []

        def source_of(self, name: str) -> str:
            """Fichier d'où vient la dernière version existante (en-tête « split from … »)."""
            path = schema_file(self.out, self.fn(name), self.versions(name)[-1])
            m = _SPLIT_FROM.search(path.read_text(encoding="utf-8"))
            return m["source"].strip() if m else "?"

        def prepare_group(self, group: list[str]) -> None:
            members = set(group)
            for _ in range(len(group) + 1):  # renommer un membre d'un cycle change le contenu des autres
                disk = {self.fn(name) for name in group}
                renamed_now = False
                for name in group:
                    if name in self.final or not self.versions(name):
                        continue  # déjà renommé, ou nouveau
                    latest = self.versions(name)[-1]
                    existing, draft = self.read(name, latest), self.content(name, undecided=members)
                    source = self.source_of(name)
                    if shape(existing, only=disk) == shape(draft, only=disk):
                        self.notes.append(f"✔ {name} : identique à {file_name(name, latest)[:-5]} "
                                          f"(créé depuis {source}), vérifié détail par détail -> réutilisé")
                        continue
                    if api_name(source) == api:
                        continue  # même API : le schéma évolue, nouvelle version
                    _, base, kind = schema_naming(name)
                    base = origins[name][0] if name in origins else base
                    new_name, new_base = renamed_for_api(name, base, api)
                    self.final[name] = new_name
                    self.origin_override[new_name] = (new_base, origins.get(name, (base, kind))[1])
                    self.notes.append(f"✎ {name} : diffère de {file_name(name, latest)[:-5]} "
                                      f"(créé depuis {source}) -> renommé {new_name}")
                    diff = schema_diff(existing, draft)
                    lines = [f"{mark} {item}" for key, mark in _DIFF_MARKS for item in diff[key]]
                    self.notes += [f"    {line}" for line in lines[:12]]
                    if len(lines) > 12:
                        self.notes.append(f"    … (+{len(lines) - 12} différence(s))")
                    renamed_now = True
                if not renamed_now:
                    return

    return CheckingSplitter


def block_ref_resolver(schemas_dir: Path):
    """$ref vers le fichier commun en bloc (…/rdv-actor-schemas.yaml#/components/schemas/S_X)
    -> ../XObject/XObject_V<dernière>.yaml#/components/schemas/XObject."""
    def to_ref(original: str) -> str:
        name = schema_naming(original)[0]
        versions = existing_versions(schemas_dir, name)
        if not versions:
            raise ValueError(f"{original} (fichier commun) : {name} absent de {schemas_dir} (lancez split_openapi.py)")
        return f"../{name}/{file_name(name, versions[-1])}{fragment(name)}"
    return lambda schema: map_block_refs(schema, to_ref)


def extract(apis: list[Path], schemas_dir: Path, apis_dir: Path, forced_major: set[str], dry_run: bool) -> int:
    targets, older = (apis, []) if apis else discover_apis(apis_dir, schemas_dir)
    tag = "[simulation] " if dry_run else ""
    print(f"{tag}Extraction des schémas de {len(targets)} API (dernière version de chaque API) -> {schemas_dir}/")

    extracted, failed, used_major = [], [], set()
    for api_file in targets:
        label = display(api_file, apis_dir)
        try:
            doc = load(api_file)
            inline = ((doc or {}).get("components") or {}).get("schemas") or {}
            if not inline:
                print(f"  ✔ {label} : aucun schéma défini dans l'API (déjà extrait)")
                continue
            schemas, origins = prepare_schemas(inline, label, block_ref_resolver(schemas_dir))
            here = {name for name in forced_major if schema_naming(name)[0] in schemas}
            used_major |= here
            print()
            split_schemas(schemas, origins, schemas_dir, here, dry_run, label,
                          f"  → {label} : {len(inline)} schéma(s) défini(s) dans l'API", strict=True,
                          splitter_class=checking_splitter(api_name(label), origins))
            extracted.append(api_file)
        except ValueError as error:
            print(f"  ✖ {label} : {error}")
            failed.append(label)
    if older:
        print(f"  · anciennes versions ignorées ({len(older)}) : "
              + ", ".join(display(file.parent, apis_dir) for file in older))
    unused = sorted(forced_major - used_major)
    if unused:
        print(f"  ⚠ --major : schéma(s) trouvé(s) dans aucune API : {', '.join(unused)}")

    print(f"{tag}Bilan de l'extraction : {len(extracted)} API extraite(s), "
          f"{len(targets) - len(extracted) - len(failed)} sans schéma à extraire, {len(failed)} en erreur")
    if dry_run:
        print("[simulation] Synchronisation non simulée : relancez sans --dry-run pour extraire puis synchroniser.")
        return 1 if failed else 0
    if not extracted:
        return 1 if failed else 0

    print()
    code = run(schemas_dir, apis_dir, extracted, "write")  # synchronise les API qui viennent d'être extraites
    return 1 if failed or code else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrait les schémas des API vers le dossier des schémas, puis synchronise ces API.")
    parser.add_argument("apis", type=Path, nargs="*",
                        help="API à traiter (défaut : la dernière version de chaque <dossier des API>/<api>/v<x>/<api>.yaml)")
    parser.add_argument("--schemas-dir", type=Path, default=SCHEMAS_DIR, help=f"défaut : {SCHEMAS_DIR}")
    parser.add_argument("--apis-dir", type=Path, default=APIS_DIR, help=f"défaut : {APIS_DIR}")
    parser.add_argument("--major", default="", help="schémas ayant un changement cassant, séparés par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="affiche ce qui serait extrait sans rien écrire")
    args = parser.parse_args()
    try:
        sys.exit(extract(args.apis, args.schemas_dir, args.apis_dir,
                         {name for name in args.major.split(",") if name}, args.dry_run))
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Extrait les schémas définis dans les API vers le dossier des schémas commun, puis synchronise ces API.

À lancer après split_openapi.py. Pour la dernière version de chaque API
(<dossier des API>/<api>/v<x>/<api>.yaml, x le plus grand) :

  1. les schémas de components.schemas sont découpés dans <dossier des schémas>/<Nom>/<Nom>_V<x.y>.yaml,
     exactement comme split_openapi.py : même renommage (S_Actor -> ActorObject, Actor -> Actor,
     E_Actor_Input -> ActorInput), même format de fichier, même versionnement (identique -> réutilisé,
     modifié -> nouvelle version, cascade, cycles). Plus strict que split_openapi.py : comme l'API est
     synchronisée aussitôt, un changement qui semble cassant sans --major bloque l'API (rien n'est écrit
     pour elle), sinon les autres API qui utilisent ce schéma recevraient ce changement ;
  2. l'API est aussitôt synchronisée (comme sync_schemas.py) : ses $ref pointent vers ces fichiers et
     components.schemas est retiré de l'API.

Les anciennes versions d'une API (v1, v2 quand v3 existe) ne sont jamais modifiées.
Une API sans schéma défini (déjà extraite) est simplement indiquée « à jour ».

Usage :
    python3 extract_api_schemas.py [api.yaml ...] [--schemas-dir D] [--apis-dir D] [--major Nom,Nom] [--dry-run]

    Sans argument : SCHEMAS_DIR et APIS_DIR de openapi_common.py (configuration du projet).
    api.yaml ...   API à traiter (défaut : la dernière version de chaque API)
    --major        schémas ayant un changement cassant (ancien ou nouveau nom)
    --dry-run      affiche ce qui serait extrait, sans rien écrire ni synchroniser

Dépendances : PyYAML, et openapi_common.py, split_openapi.py, sync_schemas.py à côté de ce script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import APIS_DIR, SCHEMAS_DIR, load, schema_naming  # noqa: E402
from split_openapi import prepare_schemas, split_schemas  # noqa: E402
from sync_schemas import discover_apis, display, run  # noqa: E402


def extract(apis: list[Path], schemas_dir: Path, apis_dir: Path, forced_major: set[str], dry_run: bool) -> int:
    targets, older = (apis, []) if apis else discover_apis(apis_dir, schemas_dir)
    tag = "[simulation] " if dry_run else ""
    print(f"{tag}Extraction des schémas de {len(targets)} API (dernière version de chaque API) -> {schemas_dir}/")

    extracted, failed, used_major = [], [], set()
    for api in targets:
        label = display(api, apis_dir)
        try:
            doc = load(api)
            inline = ((doc or {}).get("components") or {}).get("schemas") or {}
            if not inline:
                print(f"  ✔ {label} : aucun schéma défini dans l'API (déjà extrait)")
                continue
            schemas, origins = prepare_schemas(inline, label)
            here = {name for name in forced_major if schema_naming(name)[0] in schemas}
            used_major |= here
            print()
            split_schemas(schemas, origins, schemas_dir, here, dry_run, label,
                          f"  → {label} : {len(inline)} schéma(s) défini(s) dans l'API", strict=True)
            extracted.append(api)
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

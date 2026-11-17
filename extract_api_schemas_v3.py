#!/usr/bin/env python3
"""
v3 — Extrait les composants d'une API vers le dossier unique des composants, les retire de l'API,
puis synchronise l'API.

    python extract_api_schemas_v3.py <api.yaml>

1. Les parameters écrits dans les opérations sont vérifiés un par un : identiques à un parameter déjà
   découpé (quel que soit son nom) ou à un parameter de components de l'API -> réutilisés ; sinon ajoutés
   aux composants à extraire sous Query + leur nom (userId -> QueryUserId ; deux variantes : QueryUserId2).
2. schemas, parameters, responses et securitySchemes de components sont découpés comme le split, avec une
   vérification détaillée quand le nom existe déjà : identique -> réutilisé ; différent et venant d'une
   autre source -> renommé avec le nom de l'API (ResponseBadRequestRdvAdminActor) ; même API -> nouvelle
   version (majeure si cassante, annoncée par un message).
3. Les composants extraits sont retirés de l'API et leurs usages pointent vers les fichiers ; les
   securitySchemes restent par leur nom (la section security: les désigne ainsi), avec une $ref.
4. L'API est synchronisée.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common_v3 import (  # noqa: E402
    LOCAL, SCHEMAS_DIR, SECTIONS, SPLIT_REF, api_name, dump, file_ref, load, map_refs, naming, relpath, shape,
    to_sibling_refs)
from split_openapi_v3 import split_components  # noqa: E402
from sync_schemas_v3 import Catalog, check_latest, display, resolve_block_refs, run  # noqa: E402

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def is_extracted(value) -> bool:
    """Entrée qui n'est plus qu'une $ref vers un fichier découpé (securityScheme gardé par son nom)."""
    return isinstance(value, dict) and set(value) == {"$ref"} and bool(SPLIT_REF.search(str(value["$ref"])))


def lift_parameters(doc: dict, catalog: Catalog, folder: str) -> list[str]:
    """Parameters écrits dans les opérations (niveau chemin et opération) -> $ref, après vérification."""
    lists = [owner["parameters"] for path in (doc.get("paths") or {}).values() if isinstance(path, dict)
             for owner in [path] + [path.get(m) for m in METHODS]
             if isinstance(owner, dict) and isinstance(owner.get("parameters"), list)]
    inline = [(lst, i) for lst in lists for i, p in enumerate(lst) if isinstance(p, dict) and "$ref" not in p]
    if not inline:
        return []
    params = doc.setdefault("components", {}).setdefault("parameters", {})
    in_api = {shape(v): k for k, v in params.items() if isinstance(v, dict) and "$ref" not in v}
    on_disk = {}
    for name, versions in catalog.versions.items():
        if catalog.section(name) == "parameters":
            on_disk.setdefault(shape(catalog.content(name, versions[-1])), (name, versions[-1]))
    taken = {naming("parameters", k) for k in params}
    counts: dict[str, int] = {}
    for lst, i in inline:
        param, form = lst[i], shape(lst[i])
        if form in in_api:
            key = in_api[form]
            counts[f"✔ identique à components.parameters.{key} de l'API -> réutilisé"] = \
                counts.get(f"✔ identique à components.parameters.{key} de l'API -> réutilisé", 0) + 1
            lst[i] = {"$ref": f"#/components/parameters/{key}"}
            continue
        if (sibling := shape(to_sibling_refs(param))) in on_disk:
            name, version = on_disk[sibling]
            note = f"✔ identique à {name}_V{version[0]}.{version[1]} (déjà découpé), vérifié détail par détail -> réutilisé"
            counts[note] = counts.get(note, 0) + 1
            lst[i] = {"$ref": file_ref(name, version, "parameters", folder)}
            continue
        base, n = str(param.get("name") or "param"), 1
        key = base
        while key in params or naming("parameters", key) in taken:
            n += 1
            key = f"{base}_{n}"
        params[key], in_api[form] = param, key
        taken.add(naming("parameters", key))
        counts[f"+ {key} -> {naming('parameters', key)}" + (" (autre variante du même nom)" if n > 1 else "")] = 1
        lst[i] = {"$ref": f"#/components/parameters/{key}"}
    return [f"↥ {len(inline)} parameter(s) écrit(s) dans les opérations :"] + [
        f"    {note} ({count}×)" for note, count in counts.items()]


def extract(api: Path) -> None:
    label = display(api)
    doc = load(api)
    if not isinstance(doc, dict):
        raise ValueError(f"{label} : fichier YAML OpenAPI attendu")
    catalog = Catalog(SCHEMAS_DIR)
    folder = relpath(SCHEMAS_DIR, api.parent)
    doc, _ = resolve_block_refs(doc, catalog, api.parent)
    notes = lift_parameters(doc, catalog, folder)
    components = doc.get("components") or {}
    present = {s: items for s in SECTIONS
               if (items := {k: v for k, v in (components.get(s) or {}).items() if not is_extracted(v)})}

    print(f"Extraction de {label} -> {SCHEMAS_DIR}/")
    for line in notes:
        print(f"  {line}")
    if not present:
        print("  ✔ aucun composant défini dans l'API (déjà extrait)")
    else:
        print(f"  → à extraire : {', '.join(f'{s} {len(i)}' for s, i in present.items())}")
        result = split_components(present, SCHEMAS_DIR, label, relpath(api, SCHEMAS_DIR / "X"), api_name(label))

        # Retrait des composants extraits ; chaque usage pointe vers son fichier.
        for section in present:
            rest = {k: v for k, v in components[section].items() if k not in present[section]}
            if section == "securitySchemes":
                rest = {k: ({"$ref": file_ref(*result[section][k], section, folder)} if k in present[section] else v)
                        for k, v in components[section].items()}
            if rest:
                components[section] = rest
            else:
                del components[section]

        def replace(value: str, _) -> str:
            m = LOCAL.match(value)
            if m and m["name"] in result.get(m["section"], {}) and m["section"] != "securitySchemes":
                return file_ref(*result[m["section"]][m["name"]], m["section"], folder)
            return value
        doc = map_refs(doc, replace)
        removed = sum(len(i) for s, i in present.items() if s != "securitySchemes")
        print(f"  ✂ {removed} composant(s) retiré(s) de l'API"
              + (", securitySchemes gardés par nom avec $ref" if "securitySchemes" in present else ""))
    if not doc.get("components"):
        doc.pop("components", None)
    if notes or present:
        api.write_text(dump(doc), encoding="utf-8")
    print()


def main() -> None:
    args = sys.argv[1:]
    if len(args) != 1 or args[0].startswith("-"):
        sys.exit("Usage : python extract_api_schemas_v3.py <api.yaml>")
    api = Path(args[0])
    try:
        check_latest(api)
        SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
        extract(api)
        sys.exit(run([api], "API extraite"))
    except (ValueError, OSError) as error:
        sys.exit(f"✖ {error}")


if __name__ == "__main__":
    main()
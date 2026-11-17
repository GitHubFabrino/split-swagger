"""
Outils communs aux trois scripts de la v3 (indépendants de la v1 et de la v2). Dépendance : PyYAML.

Contient seulement ce qui sert à plusieurs scripts : configuration, lecture/écriture YAML 1.2,
références ($ref, fichiers découpés, fichier en bloc), nommage, différences et changements cassants.
    split_openapi_v3.py        préparation, choix des versions, écriture des fichiers
    sync_schemas_v3.py         catalogue, API, fichier en bloc, CHANGELOG, synchronisation
    extract_api_schemas_v3.py  extraction d'une API (utilise le split et le sync)

Dossier unique des composants : <SCHEMAS_DIR>/<Nom>/<Nom>_V<x>.<y>.yaml, contenu sous components.<section>.<Nom>.
    schemas         règles de renommage (S_Address -> AddressObject, E_Actor_Input -> ActorInput …)
    parameters      Query + nom      (actorIdParam -> QueryActorIdParam)
    responses       Response + nom   (BadRequest -> ResponseBadRequest)
    securitySchemes Security + nom   (oauth2 -> SecurityOauth2)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

import yaml

# ================================================================ CONFIGURATION DU PROJET
# Chemins relatifs à la racine du projet (dossier depuis lequel on lance les scripts).

SOURCE_FILE = Path("swaggers/RDV/schemas/rdv-actor-schemas.yaml")  # fichier de composants en bloc à découper
SCHEMAS_DIR = Path("swaggers/RDV/schemas")                          # sortie : un dossier par composant
APIS_DIR = Path("swaggers/RDV")                                     # API : <api>/v<x>/<api>.yaml
BLOCK_API = "rdv-actor"     # API dont info.version suit celle du fichier en bloc à chaque split (dernière version)

SECTIONS = ("schemas", "parameters", "responses", "securitySchemes")

# ================================================================ YAML 1.2 (fidèle à OpenAPI 3)
# PyYAML suit YAML 1.1 : NO/off -> booléens, 2024-01-01 -> date, 0755 -> 493. Ici seuls true/false sont
# des booléens, une date reste du texte, et un texte ambigu est écrit entre guillemets.


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first in (
    ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    ("float", r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
     list("-+0123456789.")),
):
    Loader.add_implicit_resolver(f"tag:yaml.org,2002:{_tag}", re.compile(_pattern), _first)
Loader.add_constructor("tag:yaml.org,2002:bool", lambda loader, node: loader.construct_scalar(node).lower() == "true")
Loader.add_constructor("tag:yaml.org,2002:int", lambda loader, node: (
    lambda t: int(t[2:], 8) if t.startswith("0o") else int(t[2:], 16) if t.startswith("0x") else int(t, 10))(
    loader.construct_scalar(node)))

_AMBIGUOUS = re.compile(
    r"^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE|y|Y|yes|Yes|YES|n|N|no|No|NO|on|On|ON|off|Off|OFF"
    r"|[-+]?[0-9][0-9_:]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?|[-+]?\.[0-9]+(?:[eE][-+]?[0-9]+)?"
    r"|0[bo][0-7_]+|0x[0-9a-fA-F_]+|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|\d{4}-\d\d?-\d\d?(?:[Tt ].*)?)$")


class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)  # « allOf:\n    - $ref » plutôt que « allOf:\n- $ref »


Dumper.add_representer(str, lambda dumper, text: dumper.represent_scalar(
    "tag:yaml.org,2002:str", text, style="|" if "\n" in text else "'" if _AMBIGUOUS.match(text) else None))


def load(path: Path):
    """Lit un fichier YAML ; une erreur donne le fichier, la ligne, la colonne et une piste de correction."""
    try:
        return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=Loader)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f", ligne {mark.line + 1}, colonne {mark.column + 1}" if mark else ""
        problem = getattr(error, "problem", None) or str(error).splitlines()[0]
        hint = ""
        if mark and mark.buffer and mark.line < len(mark.buffer.splitlines()):
            line = mark.buffer.splitlines()[mark.line].strip()
            value = line.split(":", 1)[1].strip() if ":" in line else line
            if value[:1] in "&*!%@`":
                hint = f"\n    → « {line} » : une valeur qui commence par {value[0]} doit être entre guillemets"
        raise ValueError(f"YAML invalide dans {path}{where} : {problem}{hint}") from None


def dump(obj, indent: int = 2) -> str:
    return yaml.dump(obj, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100, indent=indent)


# ================================================================ références

LOCAL = re.compile(r"^#/components/(?P<section>[A-Za-z]+)/(?P<name>[^/]+)$")
# Fichier découpé : …/Nom/Nom_V1.2.yaml(#/components/<section>/Nom)
SPLIT_REF = re.compile(r"(?:^|[\\/])(?P<name>[^/\\#]+)[\\/](?P=name)_V(?P<major>\d+)\.(?P<minor>\d+)\.yaml"
                       r"(?:#/components/(?P<section>[A-Za-z]+)/(?P=name))?$")
# Fichier en bloc : …/rdv-actor-schemas.yaml#/components/<section>/<Nom> (pas un fichier découpé)
BLOCK_REF = re.compile(r"^(?P<file>[^#]+\.ya?ml)#/components/(?P<section>[A-Za-z]+)/(?P<name>[^/#]+)$")


def is_annotation(key) -> bool:
    """Extensions x-* : recopiées telles quelles, sauf x-webhooks dont les $ref sont résolues par les outils."""
    return isinstance(key, str) and key.startswith("x-") and key != "x-webhooks"


def map_refs(node, replace: Callable[[str, bool], str]):
    """Copie de node où chaque référence ($ref, valeur de discriminator.mapping) vaut replace(valeur, mapping).
    mapping=True pour discriminator.mapping, où un nom seul (« Cat ») désigne un schéma."""
    if isinstance(node, list):
        return [map_refs(item, replace) for item in node]
    if not isinstance(node, dict):
        return node
    copy = {}
    for key, value in node.items():
        if is_annotation(key):
            copy[key] = value
        elif key == "$ref" and isinstance(value, str):
            copy[key] = replace(value, False)
        elif key == "discriminator" and isinstance(value, dict) and isinstance(value.get("mapping"), dict):
            copy[key] = {**value, "mapping": {k: replace(v, True) if isinstance(v, str) else v
                                              for k, v in value["mapping"].items()}}
        else:
            copy[key] = map_refs(value, replace)
    return copy


def all_refs(node) -> list[str]:
    found: list[str] = []
    map_refs(node, lambda value, _: found.append(value) or value)
    return found


def file_name(name: str, version) -> str:
    return f"{name}_V{version[0]}.{version[1]}.yaml"


def file_ref(name: str, version, section: str, folder: str = "..") -> str:
    return f"{folder}/{name}/{file_name(name, version)}#/components/{section}/{name}"


def to_sibling_refs(node):
    """$ref vers un fichier découpé -> ../Nom/Nom_Vx.y.yaml#/components/<section>/Nom (vu depuis un dossier voisin)."""
    def replace(value: str, _) -> str:
        m = SPLIT_REF.search(value)
        return file_ref(m["name"], (m["major"], m["minor"]), m["section"] or "schemas") if m else value
    return map_refs(node, replace)


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def shape(obj, only: set[str] | None = None) -> str:
    """Contenu comparable : sans les numéros de version des fichiers référencés (only : seulement ceux-là)."""
    text = canon(obj)
    if only is None:
        return re.sub(r"_V\d+\.\d+\.yaml", ".yaml", text)
    for name in only:
        text = re.sub(rf"/{re.escape(name)}/{re.escape(name)}_V\d+\.\d+\.yaml", f"/{name}/{name}.yaml", text)
    return text


def ref_versions(content) -> dict[str, tuple[int, int]]:
    """{Nom: (majeure, mineure)} des fichiers découpés référencés."""
    return {n: (int(a), int(b)) for n, a, b in re.findall(r"/([^/\"]+)/\1_V(\d+)\.(\d+)\.yaml", canon(content))}


# ================================================================ nommage

_PREFIX = re.compile(r"^[A-Z]+_")                                          # S_, E_, A_ …
_SEPARATORS = re.compile(r"[_\-.\s]+")
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z0-9]+")
SECTION_PREFIX = {"parameters": "Query", "responses": "Response", "securitySchemes": "Security"}
def pascal_case(text: str) -> str:
    return "".join(w[0].upper() + w[1:] for part in _SEPARATORS.split(text) for w in _WORDS.findall(part))


def schema_naming(name: str) -> tuple[str, str, str]:
    """(nouveau nom, base, type) d'un schéma : préfixe S_/E_/A_ retiré, PascalCase, Input/Output à la fin,
    « Object » ajouté seulement si le nom avait un préfixe. S_ActorCompany -> ActorCompanyObject,
    E_ActorComposite_Input -> ActorCompositeInput, ActorCompany -> ActorCompany."""
    prefixed = bool(_PREFIX.match(name))
    words = [w for part in _SEPARATORS.split(_PREFIX.sub("", name)) for w in _WORDS.findall(part)]
    lowered = [w.lower() for w in words]
    kind = "Input" if "input" in lowered else "Output" if "output" in lowered else "Object"
    base = "".join(w[0].upper() + w[1:] for w in words if w.lower() not in ("input", "output"))
    if not base:
        raise ValueError(f"Nom de schéma sans partie utile après renommage : {name}")
    if kind != "Object":
        return base + kind, base, kind
    if not prefixed:
        return base, base, kind
    if base.endswith("Object") and base != "Object":
        base = base[:-len("Object")]
    return base + kind, base, kind


def naming(section: str, name: str) -> str:
    """Nom dans le dossier des composants."""
    if section == "schemas":
        return schema_naming(name)[0]
    prefix, pascal = SECTION_PREFIX[section], pascal_case(name)
    if not pascal:
        raise ValueError(f"{section}.{name} : nom impossible à convertir")
    return pascal if pascal.startswith(prefix) and pascal[len(prefix):][:1].isupper() else prefix + pascal


def api_name(label: str) -> str:
    """'rdv-admin-actor/v1/rdv-admin-actor.yaml' -> 'rdv-admin-actor'."""
    parts = [p for p in re.split(r"[\\/]", label) if p]
    return parts[0] if parts else ""


def renamed_for_api(name: str, base: str, api: str) -> tuple[str, str]:
    """Nom propre à une API, inséré avant le suffixe : AddressObject -> AddressRdvAdminActorObject."""
    tail = name[len(base):] if name.startswith(base) else ""
    return base + pascal_case(api) + tail, base + pascal_case(api)


# ================================================================ différences et changements cassants

_BREAKING = {
    "fr": {"type": "{at} : type modifié", "enum": '{at} : valeur d\'enum "{value}" retirée',
           "required": "{at} : devenu obligatoire", "removed": "{at} : supprimé", "changed": "{at} : modifié",
           "root": "racine"},
    "en": {"type": "`{at}`: type changed", "enum": "`{at}`: enum value `{value}` removed",
           "required": "`{at}`: now required", "removed": "`{at}`: removed", "changed": "`{at}`: changed",
           "root": "(schema)"},
}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def breaking_changes(old, new, at: str = "", lang: str = "fr") -> list[str]:
    """Changements cassants : champ supprimé ou devenu obligatoire, type modifié, valeur d'enum retirée,
    parameter renommé/déplacé ou devenu obligatoire."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    text, prefix = _BREAKING[lang], f"{at}." if at else ""
    here = at or text["root"]
    found = []
    if "type" in old and "type" in new and canon(old["type"]) != canon(new["type"]):
        found.append(text["type"].format(at=here))
    found += [text["enum"].format(at=here, value=v) for v in _as_list(old.get("enum"))
              if v not in _as_list(new.get("enum"))]
    if isinstance(new.get("required"), bool):
        if new["required"] and not old.get("required"):
            found.append(text["required"].format(at=here))
    else:
        found += [text["required"].format(at=f"{prefix}{r}") for r in _as_list(new.get("required"))
                  if r not in _as_list(old.get("required"))]
    found += [text["changed"].format(at=f"{prefix}{k}") for k in ("name", "in")
              if k in old and k in new and old[k] != new[k]]
    if isinstance(old.get("schema"), dict):
        found += breaking_changes(old["schema"], new.get("schema"), at, lang)
    new_properties = new.get("properties") or {}
    for key, sub in (old.get("properties") or {}).items():
        found += ([text["removed"].format(at=f"{prefix}{key}")] if key not in new_properties
                  else breaking_changes(sub, new_properties[key], f"{prefix}{key}", lang))
    new_all_of = new.get("allOf") or []
    for i, sub in enumerate(old.get("allOf") or []):
        found += breaking_changes(sub, new_all_of[i] if i < len(new_all_of) else None, at, lang)
    if "items" in old:
        found += breaking_changes(old["items"], new.get("items"), f"{at}[]", lang)
    return found


def _summary(value) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            m = re.search(r"([^/#]+)_V(\d+\.\d+)\.yaml", value["$ref"])
            return f"→ {m[1]} {m[2]}" if m else f"→ {value['$ref']}"
        kind = value.get("type")
        if kind == "array" and isinstance(value.get("items"), dict):
            return f"array of {_summary(value['items'])}"
        if kind:
            return (" | ".join(kind) if isinstance(kind, list) else kind) + (
                f" ({value['format']})" if value.get("format") else "")
        return "object"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 60 else text[:57] + "…"


def _label(path: list) -> str:
    parts: list[str] = []
    for step in path:
        if step == "properties":
            continue
        if step == "items":
            parts.append((parts.pop() if parts else "") + "[]")
        else:
            parts.append(str(step))
    return ".".join(parts) or "(schema)"


def schema_diff(old, new) -> dict[str, list[str]]:
    """Différences lisibles (anglais) : {added, removed, changed, dependencies, breaking}."""
    result = {"added": [], "removed": [], "changed": [], "dependencies": [], "breaking": []}

    def walk(a, b, path: list) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            if isinstance(a.get("$ref"), str) and isinstance(b.get("$ref"), str) and a["$ref"] != b["$ref"]:
                ra, rb = _summary(a)[2:], _summary(b)[2:]
                if ra.split(" ")[0] == rb.split(" ")[0]:
                    result["dependencies"].append(f"`{ra.split(' ')[0]}`: {ra.split(' ')[-1]} → {rb.split(' ')[-1]}")
                else:
                    result["changed"].append(f"`{_label(path)}`: reference changed from {ra} to {rb}")
            for key in a.keys() | b.keys():
                if key == "$ref":
                    continue
                if key in ("required", "enum") and all(isinstance(x.get(key, []), list) for x in (a, b)):
                    old_set, new_set = a.get(key) or [], b.get(key) or []
                    for item in old_set:
                        if item not in new_set:
                            (result["changed"] if key == "required" else result["removed"]).append(
                                f"`{_label(path + [item])}`: no longer required" if key == "required"
                                else f"`{_label(path)}`: enum value `{item}` removed")
                    for item in new_set:
                        if item not in old_set:
                            (result["changed"] if key == "required" else result["added"]).append(
                                f"`{_label(path + [item])}`: now required" if key == "required"
                                else f"`{_label(path)}`: enum value `{item}` added")
                elif key not in b:
                    result["removed"].append(f"`{_label(path + [key])}` ({_summary(a[key])})")
                elif key not in a:
                    result["added"].append(f"`{_label(path + [key])}` ({_summary(b[key])})")
                else:
                    walk(a[key], b[key], path + [key])
        elif isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                if i >= len(b):
                    result["removed"].append(f"`{_label(path + [i])}` ({_summary(a[i])})")
                elif i >= len(a):
                    result["added"].append(f"`{_label(path + [i])}` ({_summary(b[i])})")
                else:
                    walk(a[i], b[i], path + [i])
        elif a != b:
            result["changed"].append(f"`{_label(path)}`: {_summary(a)} → {_summary(b)}")

    walk(old, new, [])
    result["breaking"] = breaking_changes(old, new, lang="en")
    return {key: sorted(dict.fromkeys(items)) for key, items in result.items()}


# ================================================================ fichiers découpés

def existing_versions(out: Path, name: str) -> list[tuple[int, int]]:
    folder = out / name
    if not folder.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(name)}_V(\d+)\.(\d+)\.yaml$")
    return sorted((int(m[1]), int(m[2])) for f in folder.iterdir() if (m := pattern.match(f.name)))


def relpath(target: Path, start: Path) -> str:
    """Chemin relatif avec des / (aussi sous Windows)."""
    return os.path.relpath(target.resolve(), start.resolve()).replace(os.sep, "/")


# ================================================================ info.version

_INFO_VERSION = re.compile(r"(?m)^(info:[ \t]*\n(?:[ \t]+.*\n|[ \t]*\n)*?[ \t]+version:[ \t]*)(['\"]?)(?P<v>[^'\"\n#]+?)\2[ \t]*$")
_INFO_VERSION_FLOW = re.compile(r"(?m)^info:[ \t]*\{[^}]*?\bversion:[ \t]*(['\"]?)(?P<v>[^,'\"}\s]+)\1")


def info_version(text: str) -> tuple[str | None, re.Match | None]:
    """(info.version, emplacement dans le texte) ; info sur plusieurs lignes ou sur une seule (info: {…})."""
    m = _INFO_VERSION.search(text) or _INFO_VERSION_FLOW.search(text)
    return (m["v"].strip(), m) if m else (None, None)


def with_info_version(text: str, version: str) -> str:
    """Texte avec info.version remplacée (tel quel si info.version est absente)."""
    _, m = info_version(text)
    if not m:
        return text
    value = f"'{version}'" if re.fullmatch(r"\d+(\.\d+)?", version) and not m[2 if m.re is _INFO_VERSION else 1] \
        else version                                                   # 1.15 resterait sinon un nombre
    return text[:m.start("v")] + value + text[m.end("v"):]


def version_tuple(version: str | None) -> tuple[int, int, int]:
    parts = [int(p) if p.isdigit() else 0 for p in (str(version or "0").split(".") + ["0", "0"])[:3]]
    return parts[0], parts[1], parts[2]
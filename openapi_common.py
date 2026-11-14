"""
Outils communs à split_openapi.py et sync_schemas.py.

1. YAML fidèle à OpenAPI 3 (YAML 1.2, schéma « core »). PyYAML suit YAML 1.1, qui altère
   des valeurs courantes : NO/off/yes -> booléens, 2024-01-01 -> date, 12:30 -> 750, 0755 -> 493.
   Ici seules true/false sont des booléens, une date reste du texte, et tout texte qui serait
   relu autrement est écrit entre guillemets.
2. Références vers les schémas : un seul parcours, map_refs, qui connaît les règles
   ($ref, discriminator.mapping, extensions x-*), et ce qui s'en déduit.

Dépendance : PyYAML (pip install pyyaml)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import yaml

# ================================================================ CONFIGURATION DU PROJET
# À adapter une fois. Chemins relatifs au dossier depuis lequel on lance les scripts
# (la racine du projet, ex. wma-api-gouv-RDV/). Chacun peut être remplacé en ligne de commande.

SOURCE_FILE = Path("swaggers/RDV/schemas/rdv-actor-schemas.yaml")  # fichier de schémas en bloc à découper
SCHEMAS_DIR = Path("swaggers/RDV/schema")                           # sortie : un dossier par schéma (créé automatiquement)
APIS_DIR = Path("swaggers/RDV")                                     # où chercher les API : <api>/v<x>/<api>.yaml

# ================================================================ YAML 1.2

_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")
_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")
_FLOAT = re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
                    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = {}
for _tag, _pattern, _first_chars in (
    ("tag:yaml.org,2002:null", _NULL, ["~", "n", "N", ""]),
    ("tag:yaml.org,2002:bool", _BOOL, list("tTfF")),
    ("tag:yaml.org,2002:int", _INT, list("-+0123456789")),
    ("tag:yaml.org,2002:float", _FLOAT, list("-+0123456789.")),
):
    Loader.add_implicit_resolver(_tag, _pattern, _first_chars)


def _construct_int(loader, node):
    text = loader.construct_scalar(node)
    if text.startswith("0o"):
        return int(text[2:], 8)
    if text.startswith("0x"):
        return int(text[2:], 16)
    return int(text, 10)  # « 0755 » vaut 755 en YAML 1.2, pas 493


def _construct_bool(loader, node):
    return loader.construct_scalar(node).lower() == "true"


Loader.add_constructor("tag:yaml.org,2002:int", _construct_int)
Loader.add_constructor("tag:yaml.org,2002:bool", _construct_bool)

# Textes qu'un lecteur YAML 1.1 ou 1.2 prendrait pour autre chose qu'une chaîne.
_AMBIGUOUS = re.compile(
    r"^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE"
    r"|y|Y|yes|Yes|YES|n|N|no|No|NO|on|On|ON|off|Off|OFF"
    r"|[-+]?[0-9][0-9_:]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.[0-9]+(?:[eE][-+]?[0-9]+)?"
    r"|0[bo][0-7_]+|0x[0-9a-fA-F_]+"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
    r"|\d{4}-\d\d?-\d\d?(?:[Tt ].*)?)$")


class Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        # « allOf:\n    - $ref: … » plutôt que « allOf:\n- $ref: … »
        return super().increase_indent(flow, False)


def _represent_str(dumper, text):
    if "\n" in text:
        style = "|"
    elif _AMBIGUOUS.match(text):
        style = "'"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


Dumper.add_representer(str, _represent_str)


def loads(text: str):
    return yaml.load(text, Loader=Loader)


def yaml_error(path, error: yaml.YAMLError) -> ValueError:
    """Erreur YAML lisible : fichier, ligne, colonne, cause, et l'astuce pour les cas fréquents."""
    mark = getattr(error, "problem_mark", None)
    where = f", ligne {mark.line + 1}, colonne {mark.column + 1}" if mark else ""
    problem = getattr(error, "problem", None) or str(error).splitlines()[0]
    hint = ""
    if mark and mark.buffer:
        line = mark.buffer.splitlines()[mark.line] if mark.line < len(mark.buffer.splitlines()) else ""
        value = line.split(":", 1)[1].strip() if ":" in line else line.strip()
        if value[:1] in "&*!%@`":
            hint = (f"\n    → « {line.strip()} » : une valeur qui commence par {value[0]} doit être entre guillemets, "
                    f"ex. '{value}'" if "'" not in value else f"\n    → « {line.strip()} » : mettez la valeur entre guillemets")
    return ValueError(f"YAML invalide dans {path}{where} : {problem}{hint}")


def load(path: Path):
    try:
        return loads(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise yaml_error(path, error) from None


def dump(obj, indent: int = 2) -> str:
    return yaml.dump(obj, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100, indent=indent)


# ================================================================ références vers les schémas

LOCAL = "#/components/schemas/"

# Extensions x-* : annotations libres, recopiées telles quelles. Exception : celles-ci, dont
# les $ref sont résolues par les outils (x-webhooks = webhooks d'OpenAPI 3.0).
RESOLVED_EXTENSIONS = {"x-webhooks"}

# Chemin d'un fichier de schéma versionné : .../OrderObject/OrderObject_V1.2.yaml (_v1.2 accepté)
VERSION_IN_PATH = re.compile(r"_[vV]\d+\.\d+\.yaml")


def is_annotation(key) -> bool:
    return isinstance(key, str) and key.startswith("x-") and key not in RESOLVED_EXTENSIONS


def _schema_name(value, names: set, bare_allowed: bool) -> str | None:
    """Nom du schéma visé par une référence, ou None si elle ne vise pas un schéma du fichier.
    bare_allowed : un nom seul (« Cat ») est accepté, ce que permet discriminator.mapping."""
    if not isinstance(value, str):
        return None
    if value.startswith(LOCAL):
        return value[len(LOCAL):]
    if bare_allowed and value in names:
        return value
    return None


def map_refs(node, names: set, local: Callable[[str], str],
             other: Callable[[str], None] | None = None):
    """Parcourt node et renvoie une copie où chaque référence vers un schéma local
    ($ref ou valeur de discriminator.mapping) est remplacée par local(nom).
    other(valeur) est appelé pour chaque autre référence (fichier externe, autre composant).
    Les extensions x-* (sauf RESOLVED_EXTENSIONS) sont recopiées sans être parcourues."""

    def replace(value, bare_allowed: bool):
        name = _schema_name(value, names, bare_allowed)
        if name is not None:
            return local(name)
        if other and isinstance(value, str):
            other(value)
        return value

    if isinstance(node, list):
        return [map_refs(item, names, local, other) for item in node]
    if not isinstance(node, dict):
        return node

    copy = {}
    for key, value in node.items():
        if is_annotation(key):
            copy[key] = value
        elif key == "$ref":
            copy[key] = replace(value, bare_allowed=False)
        elif key == "discriminator" and isinstance(value, dict) and isinstance(value.get("mapping"), dict):
            discriminator = {}
            for d_key, d_value in value.items():
                if d_key == "mapping":
                    discriminator[d_key] = {k: replace(v, bare_allowed=True) for k, v in d_value.items()}
                else:
                    discriminator[d_key] = map_refs(d_value, names, local, other)
            copy[key] = discriminator
        else:
            copy[key] = map_refs(value, names, local, other)
    return copy


def schema_refs(node, names: set) -> set[str]:
    """Noms des schémas locaux référencés par node."""
    found: set[str] = set()

    def remember(name: str) -> str:
        found.add(name)
        return name

    map_refs(node, names, remember)
    return found


def other_refs(node, names: set) -> list[str]:
    """Références qui ne visent pas un schéma local (fichier externe, autre composant…)."""
    found: list[str] = []
    map_refs(node, names, lambda name: name, other=found.append)
    return found


# ================================================================ comparaison de schémas

def canon(obj) -> str:
    """Texte unique pour un contenu : deux schémas égaux donnent le même texte, quel que soit
    l'ordre de leurs clés."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def without_sources(obj):
    """Retire x-sources (suivi des API utilisatrices), qui ne fait pas partie du schéma."""
    return {k: v for k, v in obj.items() if k != "x-sources"} if isinstance(obj, dict) else obj


def shape(obj, only: set[str] | None = None) -> str:
    """Contenu sans x-sources ni numéros de version dans les chemins de schémas.
    only : n'efface que les versions de ces schémas (les autres restent exactes)."""
    text = canon(without_sources(obj))
    if only is None:
        return VERSION_IN_PATH.sub(".yaml", text)
    for name in only:
        text = re.sub(rf"\.\./{re.escape(name)}/{re.escape(name)}_[vV]\d+\.\d+\.yaml", f"../{name}/{name}.yaml", text)
    return text


# ================================================================ différences entre deux versions

_BREAKING_TEXT = {  # avertissements de split_openapi.py (fr) et changelog (en)
    "fr": {"type": "{at} : type modifié", "enum": '{at} : valeur d\'enum "{value}" retirée',
           "required": "{at} : devenu obligatoire", "removed": "{at} : supprimé", "root": "racine"},
    "en": {"type": "`{at}`: type changed", "enum": "`{at}`: enum value `{value}` removed",
           "required": "`{at}`: now required", "removed": "`{at}`: removed", "root": "(schema)"},
}


def breaking_changes(old, new, at: str = "", lang: str = "fr") -> list[str]:
    """Changements cassants simples (indicatif) : champ supprimé, devenu obligatoire, type, enum.
    lang : « fr » pour les messages du terminal, « en » pour le changelog."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    text = _BREAKING_TEXT[lang]
    here = at or text["root"]
    prefix = f"{at}." if at else ""
    found = []
    if "type" in old and "type" in new and canon(old["type"]) != canon(new["type"]):
        found.append(text["type"].format(at=here))
    found += [text["enum"].format(at=here, value=v) for v in old.get("enum", []) if v not in new.get("enum", [])]
    found += [text["required"].format(at=f"{prefix}{r}") for r in new.get("required", [])
              if r not in old.get("required", [])]
    new_properties = new.get("properties") or {}
    for key, sub in (old.get("properties") or {}).items():
        if key not in new_properties:
            found.append(text["removed"].format(at=f"{prefix}{key}"))
        else:
            found += breaking_changes(sub, new_properties[key], f"{prefix}{key}", lang)
    new_all_of = new.get("allOf") or []
    for i, sub in enumerate(old.get("allOf") or []):
        found += breaking_changes(sub, new_all_of[i] if i < len(new_all_of) else None, at, lang)
    if "items" in old:
        found += breaking_changes(old["items"], new.get("items"), f"{at}[]", lang)
    return found


_REF_FILE = re.compile(r"(?:^|/)([^/#]+)_[vV](\d+\.\d+)\.yaml")
_COMBINERS = ("allOf", "oneOf", "anyOf")


def ref_target(ref: str) -> tuple[str, str | None]:
    """'../AddressObject/AddressObject_V1.1.yaml#/…' -> ('AddressObject', '1.1')."""
    m = _REF_FILE.search(ref)
    return (m[1], m[2]) if m else (ref, None)


def summarize(value) -> str:
    """Short summary of a value for the changelog (in English): type, reference, or truncated text."""
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            name, version = ref_target(value["$ref"])
            return f"→ {name}" + (f" {version}" if version else "")
        kind = value.get("type")
        if kind == "array" and isinstance(value.get("items"), dict):
            return f"array of {summarize(value['items'])}"
        if kind:
            kind = " | ".join(kind) if isinstance(kind, list) else kind
            return kind + (f" ({value['format']})" if value.get("format") else "")
        for combiner in _COMBINERS:
            if combiner in value:
                return f"{combiner} of {len(value[combiner])} item(s)"
        return "object"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 60 else text[:57] + "…"


def _label(path: list) -> str:
    """['properties', 'contacts', 'items', 'properties', 'type'] -> 'contacts[].type'."""
    parts: list[str] = []
    i = 0
    while i < len(path):
        step = path[i]
        if step == "properties":
            pass
        elif step == "items":
            parts.append((parts.pop() if parts else "") + "[]")
        elif step in _COMBINERS and i + 1 < len(path) and isinstance(path[i + 1], int):
            parts.append(f"{step}[{path[i + 1]}]")
            i += 1
        else:
            parts.append(str(step))
        i += 1
    return ".".join(parts) or "(schema)"


def schema_diff(old, new) -> dict[str, list[str]]:
    """Différences lisibles (en anglais, pour le changelog) entre deux versions d'un schéma (x-sources ignoré) :
    {"added": [...], "removed": [...], "changed": [...], "dependencies": [...], "breaking": [...]}."""
    result = {"added": [], "removed": [], "changed": [], "dependencies": [], "breaking": []}

    def walk(a, b, path: list) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            if isinstance(a.get("$ref"), str) and isinstance(b.get("$ref"), str) and a["$ref"] != b["$ref"]:
                (name_a, ver_a), (name_b, ver_b) = ref_target(a["$ref"]), ref_target(b["$ref"])
                if name_a == name_b:
                    result["dependencies"].append(f"`{name_a}`: {ver_a} → {ver_b}")
                else:
                    result["changed"].append(f"`{_label(path)}`: reference changed from {name_a} to {name_b}")
            for key in a.keys() | b.keys():
                if key == "x-sources" or key == "$ref":
                    continue
                if key in ("required", "enum"):  # liste absente = liste vide : on détaille valeur par valeur
                    walk(a.get(key) or [], b.get(key) or [], path + [key])
                    continue
                if key not in b:
                    result["removed"].append(f"`{_label(path + [key])}` ({summarize(a[key])})")
                elif key not in a:
                    result["added"].append(f"`{_label(path + [key])}` ({summarize(b[key])})")
                else:
                    walk(a[key], b[key], path + [key])
        elif isinstance(a, list) and isinstance(b, list):
            if all(not isinstance(x, (dict, list)) for x in a + b):   # required, enum, type…
                key = path[-1] if path else None
                for item in a:
                    if item not in b:
                        if key == "required":
                            result["changed"].append(f"`{_label(path[:-1] + [item])}`: no longer required")
                        elif key == "enum":
                            result["removed"].append(f"`{_label(path[:-1])}`: enum value `{item}` removed")
                        else:
                            result["removed"].append(f"`{_label(path)}`: value `{item}` removed")
                for item in b:
                    if item not in a:
                        if key == "required":
                            result["changed"].append(f"`{_label(path[:-1] + [item])}`: now required")
                        elif key == "enum":
                            result["added"].append(f"`{_label(path[:-1])}`: enum value `{item}` added")
                        else:
                            result["added"].append(f"`{_label(path)}`: value `{item}` added")
            else:                                                      # allOf, oneOf… : par position
                for i in range(max(len(a), len(b))):
                    if i >= len(b):
                        result["removed"].append(f"`{_label(path + [i])}` ({summarize(a[i])})")
                    elif i >= len(a):
                        result["added"].append(f"`{_label(path + [i])}` ({summarize(b[i])})")
                    else:
                        walk(a[i], b[i], path + [i])
        elif a != b:
            result["changed"].append(f"`{_label(path)}`: {summarize(a)} → {summarize(b)}")

    walk(old, new, [])
    result["breaking"] = breaking_changes(old, new, lang="en")
    for key in result:
        result[key] = sorted(dict.fromkeys(result[key]))
    return result


# ================================================================ nommage des schémas

KINDS = ("Input", "Output", "Object")

DESCRIPTIONS = {
    "Object": "{base} - shared object, reused by Input/Output schemas",
    "Input": "{base} - request payload (create / update)",
    "Output": "{base} - response payload",
}

_PREFIX = re.compile(r"^[A-Z]+_")                      # S_, E_, A_ …
_SEPARATORS = re.compile(r"[_\-.\s]+")                # _ - . espace
_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z0-9]+")  # mots d'un nom en camelCase


def schema_naming(name: str) -> tuple[str, str, str]:
    """Nom de sortie d'un schéma : (nouveau nom, nom de base, type).
        S_ActorCompany          -> ("ActorCompanyObject", "ActorCompany", "Object")   préfixe : + Object
        ActorCompany            -> ("ActorCompany", "ActorCompany", "Object")         sans préfixe : nom gardé
        account_annual_revenue  -> ("AccountAnnualRevenue", …, "Object")              (toujours en PascalCase)
        E_ActorComposite_Input  -> ("ActorCompositeInput", "ActorComposite", "Input")
        E_Input_Address         -> ("AddressInput", "Address", "Input")
    Préfixe (S_, A_, E_ …) retiré, séparateurs supprimés, PascalCase. « Input » / « Output » sont reconnus
    comme mots entiers (pas « Throughput ») et placés à la fin, avec ou sans préfixe. Sinon, le suffixe
    « Object » n'est ajouté que si le nom avait un préfixe."""
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
        return base, base, kind  # sans préfixe : pas de suffixe Object
    if base.endswith("Object") and base != "Object":
        base = base[: -len("Object")]  # S_ActorObject -> ActorObject, pas ActorObjectObject
    return base + kind, base, kind


def rename_map(names) -> dict[str, str]:
    """{ancien nom: nouveau nom} ; erreur si deux schémas reçoivent le même nouveau nom."""
    renamed: dict[str, str] = {}
    owners: dict[str, list[str]] = {}
    for name in names:
        new = schema_naming(name)[0]
        renamed[name] = new
        owners.setdefault(new, []).append(name)
    conflicts = {new: olds for new, olds in owners.items() if len(olds) > 1}
    if conflicts:
        details = "; ".join(f"{', '.join(olds)} -> {new}" for new, olds in sorted(conflicts.items()))
        raise ValueError(f"Noms en conflit après renommage : {details}")
    return renamed
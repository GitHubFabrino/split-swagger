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
    pass


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


def load(path: Path):
    return loads(Path(path).read_text(encoding="utf-8"))


def dump(obj) -> str:
    return yaml.dump(obj, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=100)


# ================================================================ références vers les schémas

LOCAL = "#/components/schemas/"

# Extensions x-* : annotations libres, recopiées telles quelles. Exception : celles-ci, dont
# les $ref sont résolues par les outils (x-webhooks = webhooks d'OpenAPI 3.0).
RESOLVED_EXTENSIONS = {"x-webhooks"}

# Chemin d'un fichier de schéma versionné : .../Order/Order_v1.2.yaml
VERSION_IN_PATH = re.compile(r"_v\d+\.\d+\.yaml")


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
        text = re.sub(rf"\.\./{re.escape(name)}/{re.escape(name)}_v\d+\.\d+\.yaml", f"../{name}/{name}.yaml", text)
    return text

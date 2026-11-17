#!/usr/bin/env python3
"""
v3 — Découpe le fichier de composants en bloc (SOURCE_FILE) dans le dossier unique SCHEMAS_DIR :
un dossier par composant (schemas, parameters, responses, securitySchemes), une version par fichier.

    python split_openapi_v3.py

Versions : identique -> réutilisé ; modifié -> mineure (1.3 -> 1.4) ; cassant -> majeure (1.3 -> 2.0), annoncée
par un message, sans confirmation. Les composants qui utilisent un composant modifié ne changent pas de version :
sync_schemas_v3.py met ensuite leurs refs à jour (dernière version), ainsi que celles des API.
Les autres sections du fichier en bloc (examples, headers…) y restent ; les références y pointent.
Chaque fichier créé reçoit dans info.version la version du fichier en bloc (ex. 1.15.0) ; le nom du fichier
garde la version du composant (ErrorOutput_V1.1.yaml). Un schéma au contenu identique garde son fichier : seule
sa version passe à celle du bloc, avec une ligne « # updated on: <date> RDV 1.15.0 evolution (no content change) ».
L'API BLOCK_API (rdv-actor, dernière version) prend aussi la version du bloc, si elle est plus grande.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common_v3 import (  # noqa: E402
    APIS_DIR, BLOCK_API, LOCAL, SCHEMAS_DIR, SECTIONS, SOURCE_FILE, info_version, version_tuple, with_info_version, SPLIT_REF, all_refs, api_name, breaking_changes, dump,
    existing_versions, file_name, file_ref, load, map_refs, naming, ref_versions, relpath, renamed_for_api,
    schema_diff, schema_naming, shape, to_sibling_refs)

SECTION_KIND = {"parameters": "Parameter", "responses": "Response", "securitySchemes": "SecurityScheme"}
SECTION_TITLE = {"schemas": "Schema", "parameters": "Parameter", "responses": "Response",
                 "securitySchemes": "Security scheme"}
DESCRIPTIONS = {
    "Object": "{base} - shared object, reused by Input/Output schemas",
    "Input": "{base} - request payload (create / update)",
    "Output": "{base} - response payload",
    "Parameter": "{base} - reusable request parameter",
    "Response": "{base} - reusable response",
    "SecurityScheme": "{base} - security scheme",
}


# ================================================================ préparation des composants

class Item(NamedTuple):
    section: str
    content: dict      # références internes : #/components/<section>/<NomFinal>
    base: str          # pour la description du fichier
    kind: str


def prepare(components: dict, where: str, source_ref: str | None = None) -> tuple[dict[str, Item], dict]:
    """Renomme et contrôle les composants : ({Nom: Item}, {section: {nom d'origine: Nom}}).
    Références vers un composant du même fichier -> #/components/<section>/<Nom> ; vers un fichier déjà
    découpé -> chemin voisin ; vers une autre section (examples…) -> source_ref (le fichier d'origine)."""
    names: dict[str, dict[str, str]] = {}
    for section in SECTIONS:
        if components.get(section):
            names[section] = {name: naming(section, name) for name in components[section]}
    owners: dict[str, list[str]] = {}
    for section, mapping in names.items():
        for original, new in mapping.items():
            owners.setdefault(new, []).append(f"{section}.{original}")
    clashes = {new: olds for new, olds in owners.items() if len(olds) > 1}
    if clashes:
        raise ValueError(f"{where} : noms en conflit après renommage : "
                         + "; ".join(f"{', '.join(olds)} -> {new}" for new, olds in sorted(clashes.items())))

    items, problems = {}, []
    for section, mapping in names.items():
        for original, new in mapping.items():
            def replace(value: str, in_mapping: bool, at=f"{section}.{original}") -> str:
                m = LOCAL.match(value)
                if in_mapping and not m and value in names.get("schemas", {}):
                    return names["schemas"][value]           # discriminator.mapping : nom seul
                if m and m["section"] in SECTIONS:
                    if m["name"] in names.get(m["section"], {}):
                        return f"#/components/{m['section']}/{names[m['section']][m['name']]}"
                    problems.append(f"{at} -> {value} (absent)")
                elif m and source_ref:
                    return source_ref + value                 # section non découpée : reste dans le fichier d'origine
                elif not SPLIT_REF.search(value) and not (source_ref and value.startswith(source_ref + "#")):
                    problems.append(f"{at} -> {value}")
                return value
            content = to_sibling_refs(map_refs(components[section][original], replace))
            base, kind = schema_naming(original)[1:] if section == "schemas" else (new, SECTION_KIND[section])
            items[new] = Item(section, content, base, kind)
    if problems:
        raise ValueError(f"{where} : {len(problems)} référence(s) non prise(s) en charge (ex. {problems[0]})")
    return items, names


# ================================================================ versionnement et écriture

_SPLIT_FROM = re.compile(r"^# created on: \S+ split from (?P<source>.+)$", re.M)
_DIFF_MARKS = (("added", "+"), ("removed", "-"), ("changed", "~"), ("dependencies", "↻"))


def dependency_groups(graph: dict[str, set[str]]) -> list[list[str]]:
    """Composantes fortement connexes (Tarjan, itératif), dépendances d'abord : un composant seul,
    ou des composants en cycle, forment un groupe versionné ensemble."""
    edges = {name: sorted(deps) for name, deps in graph.items()}
    index, low, stack, on_stack, groups = {}, {}, [], set(), []
    for root in edges:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, start = work.pop()
            if start == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            for position in range(start, len(edges[node])):
                dep = edges[node][position]
                if dep not in index:
                    work += [(node, position + 1), (dep, 0)]
                    break
                if dep in on_stack:
                    low[node] = min(low[node], index[dep])
            else:
                if low[node] == index[node]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    groups.append(sorted(group))
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
    return groups


class Versioner:
    """Choisit la version de chaque composant, puis les écrit.
      même contenu que la dernière version         -> réutilisée (si des dépendances ont une version plus récente,
                                                      le sync mettra ses refs à jour) ;
      modifié                                      -> mineure suivante de la dernière version (1.3 -> 1.4) ;
      modifié de façon cassante                    -> majeure suivante (1.3 -> 2.0), sans confirmation ;
    tous les utilisateurs (API et composants) suivent toujours la dernière version, quel que soit son numéro.
      nouveau                                      -> 1.0.
    api (extraction) : un composant dont le nom existe déjà, créé depuis une autre source, est comparé
    détail par détail : identique -> réutilisé ; différent -> renommé avec le nom de l'API."""

    def __init__(self, items: dict[str, Item], out: Path, source: str, api: str | None = None,
                 info_version: str | None = None):
        self.items, self.out, self.source, self.api = items, out, source, api
        self.info_version = info_version          # info.version du fichier source (sinon x.y.0 du composant)
        self.final: dict[str, str] = {}               # renommage avec le nom de l'API
        self.chosen: dict[str, tuple[int, int]] = {}
        self.writes: list[tuple[str, tuple, dict, str | None]] = []
        self.refreshed: list[tuple[str, tuple, dict, dict]] = []  # refs mises à jour sur place
        self.retagged: list[tuple[str, tuple, str | None]] = []   # contenu identique, info.version mise à jour
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self._cache: dict = {}

    def fn(self, name: str) -> str:
        return self.final.get(name, name)

    def versions(self, name: str) -> list[tuple[int, int]]:
        return existing_versions(self.out, self.fn(name))

    def read(self, name: str, version) -> dict:
        disk = self.fn(name)
        if (disk, version) not in self._cache:
            doc = load(self.out / disk / file_name(disk, version))
            self._cache[(disk, version)] = doc["components"][self.items[name].section][disk]
        return self._cache[(disk, version)]

    def source_of(self, name: str) -> str:
        path = self.out / self.fn(name) / file_name(self.fn(name), self.versions(name)[-1])
        m = _SPLIT_FROM.search(path.read_text(encoding="utf-8"))
        return m["source"].strip() if m else "?"

    def deps(self, name: str) -> set[str]:
        return {m["name"] for ref in all_refs(self.items[name].content) if (m := LOCAL.match(ref))
                and m["name"] in self.items}

    def content(self, name: str, undecided: set[str] = frozenset()) -> dict:
        """Contenu tel qu'il serait écrit ; undecided : dépendances encore sans version (v0.0 provisoire)."""
        def replace(value: str, _) -> str:
            m = LOCAL.match(value)
            if not m or m["name"] not in self.items:
                return value
            version = (0, 0) if m["name"] in undecided else self.chosen[m["name"]]
            return file_ref(self.fn(m["name"]), version, m["section"])
        return map_refs(self.items[name].content, replace)

    def check_names(self, group: list[str]) -> None:
        """Extraction : un nom déjà pris par un composant différent venant d'ailleurs -> renommé avec l'API."""
        members = set(group)
        for _ in range(len(group) + 1):          # renommer un membre d'un cycle change le contenu des autres
            renamed = False
            for name in group:
                if name in self.final or not self.versions(name):
                    continue
                latest = self.versions(name)[-1]
                existing, draft, source = self.read(name, latest), self.content(name, members), self.source_of(name)
                label = file_name(name, latest)[:-5]
                if self.same_but_versions(name, latest, draft):
                    self.notes.append(f"✔ {name} : identique à {label} (créé depuis {source}), "
                                      f"vérifié détail par détail -> réutilisé")
                elif api_name(source) != self.api:
                    item = self.items[name]
                    new, new_base = renamed_for_api(name, item.base, self.api)
                    self.final[name] = new
                    self.items[name] = item._replace(base=new_base)
                    self.notes.append(f"✎ {name} : diffère de {label} (créé depuis {source}) -> renommé {new}")
                    diff = schema_diff(existing, draft)
                    lines = [f"{mark} {line}" for key, mark in _DIFF_MARKS for line in diff[key]]
                    self.notes += [f"    {line}" for line in lines[:12]]
                    if len(lines) > 12:
                        self.notes.append(f"    … (+{len(lines) - 12} différence(s))")
                    renamed = True
            if not renamed:
                return

    def same_but_versions(self, name: str, version, draft: dict) -> bool:
        """Même contenu que cette version, à la version des dépendances près (mineure ou majeure)."""
        return shape(self.read(name, version)) == shape(draft)

    def decide(self, group: list[str]) -> None:
        if self.api:
            self.check_names(group)
        members = set(group)
        draft = {n: self.content(n, members) for n in group}

        # 1. Même contenu que la dernière version (à la version des dépendances près) : pas de nouvelle version
        #    (le sync mettra ensuite ses refs à jour si une dépendance a une version plus récente).
        latest = {n: self.versions(n)[-1] for n in group if self.versions(n)}
        if len(latest) == len(group) and all(self.same_but_versions(n, latest[n], draft[n]) for n in group):
            for n in group:
                self.chosen[n] = latest[n]
            for n in group:
                old, new = ref_versions(self.read(n, latest[n])), ref_versions(self.content(n))
                moved = {d: v for d, v in new.items() if old.get(d) != v}
                self.refreshed.append((self.fn(n), latest[n], moved, old))
                self.writes.append((self.fn(n), latest[n], self.content(n), None))
                # Fichier venant de la même source, créé avec une autre version de cette source : on garde le
                # fichier (contenu identique) et on met seulement sa version à jour (« # updated on … »).
                path = self.out / self.fn(n) / file_name(self.fn(n), latest[n])
                current, _ = info_version(path.read_text(encoding="utf-8"))
                if self.info_version and current != self.info_version and self.source_of(n) == self.source:
                    self.retagged.append((self.fn(n), latest[n], current))
            return

        # 2. Nouvelle version pour tout le groupe : majeure si le changement est cassant (le numéro le signale ;
        #    tous les utilisateurs suivent quand même la dernière version).
        breaking = {n: b for n in latest if (b := breaking_changes(self.read(n, latest[n]), draft[n]))}
        is_major = bool(breaking)
        for n in group:
            if n not in latest:
                self.chosen[n] = (1, 0)
            elif is_major:
                self.chosen[n] = (latest[n][0] + 1, 0)
            else:
                self.chosen[n] = (latest[n][0], latest[n][1] + 1)

        # 3. Contenu définitif et raison.
        cycle = " ↔ ".join(self.fn(n) for n in group) if len(group) > 1 or group[0] in self.deps(group[0]) else ""
        for n in group:
            content, version = self.content(n), self.chosen[n]
            if n not in latest:
                reason = "nouveau" + (f", cycle : {cycle}" if cycle else "")
            else:
                old, before = self.read(n, latest[n]), f"{latest[n][0]}.{latest[n][1]}"
                if n in breaking:
                    why = f"changement cassant : {' ; '.join(breaking[n])}"
                elif shape(old) == shape(content):
                    why = "cascade dans le cycle"
                else:
                    why = "contenu modifié"
                reason = f"{before} -> {version[0]}.{version[1]}, {why}" + (f" ({cycle})" if cycle else "")
                if is_major:
                    self.warnings.append(
                        f"{self.fn(n)} passé en version majeure {version[0]}.0"
                        + (" (changement cassant)" if n in breaking else f" (cycle : {cycle})")
                        + f" : les API et schémas qui l'utilisent passent à la {version[0]}.0 au prochain sync.")
                if not self.api and self.source_of(n) != self.source:
                    self.warnings.append(f"{self.fn(n)} existait déjà, créé depuis {self.source_of(n)} : sa nouvelle "
                                         f"version vient de {self.source}. Si ce n'est pas le même composant, "
                                         f"renommez-le dans {self.source}.")
            self.writes.append((self.fn(n), version, content, reason))

    def run(self) -> dict[str, tuple[str, tuple[int, int]]]:
        """Décide, écrit les nouvelles versions, affiche le rapport. Renvoie {Nom: (nom final, version)}."""
        for group in dependency_groups({name: self.deps(name) for name in self.items}):
            self.decide(group)
        names = {self.fn(n): n for n in self.items}
        for disk, version, _ in self.retagged:        # contenu identique : version et date de mise à jour seulement
            path = self.out / disk / file_name(disk, version)
            lines = with_info_version(path.read_text(encoding="utf-8"), self.info_version).split("\n")
            last = max(i for i, line in enumerate(lines[:10]) if line.startswith(("# created on:", "# updated on:")))
            lines.insert(last + 1, f"# updated on: {date.today():%d/%m/%Y} RDV {self.info_version} evolution "
                                   f"(no content change)")
            path.write_text("\n".join(lines), encoding="utf-8")
        for disk, version, content, reason in self.writes:
            if reason:                                 # une version réutilisée n'est jamais réécrite
                path = self.out / disk / file_name(disk, version)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.render(names[disk], disk, version, content), encoding="utf-8")
        for line in self.notes:
            print(f"  {line}")
        for disk, version, _, reason in self.writes:
            if reason:
                print(f"  + {disk}/{file_name(disk, version)}  ({reason})")
        retagged = {disk for disk, _, _ in self.retagged}
        if self.retagged:
            olds = sorted({old or "?" for _, _, old in self.retagged})
            print(f"  ⟳ {len(self.retagged)} schéma(s) au contenu identique : info.version {', '.join(olds)} -> "
                  f"{self.info_version} (« # updated on » ajouté) : "
                  + ", ".join(file_name(d, v)[:-5] for d, v, _ in self.retagged[:12])
                  + (f", … (+{len(self.retagged) - 12})" if len(self.retagged) > 12 else ""))
        reused = [file_name(disk, version)[:-5] for disk, version, _, reason in self.writes
                  if not reason and disk not in retagged]
        if reused:
            print(f"  = réutilisés ({len(reused)}) : " + ", ".join(reused[:30])
                  + (f", … (+{len(reused) - 30})" if len(reused) > 30 else ""))
        for warning in self.warnings:
            print(f"  ⚠ {warning}")
        stale = sorted(disk for disk, _, moved, old in self.refreshed if any(
            v > old.get(d, v) for d, v in moved.items()))
        if stale:
            print(f"  → {len(stale)} composant(s) utilisent une version plus ancienne de ces composants "
                  f"({', '.join(stale[:8])}{', …' if len(stale) > 8 else ''}) : "
                  f"lancez sync_schemas_v3.py --all pour les mettre à jour, ainsi que les API.")
        return {name: (self.fn(name), self.chosen[name]) for name in self.items}

    def render(self, name: str, disk: str, version, content: dict) -> str:
        item = self.items[name]
        banner = "#" * 62
        title = ("#" + f"{SECTION_TITLE[item.section]} file information".center(60)).rstrip()
        body = dump({"components": {item.section: {disk: content}}}, indent=4)
        body = re.sub(r"^(\s*(?:-\s+)?\$ref: )(\.\./\S+)$", r"\1'\2'", body, flags=re.M)
        return (f"# created on: {date.today():%d/%m/%Y} split from {self.source}\n\n{banner}\n{title}\n{banner}\n\n"
                f"info:\n    title: {disk}\n    description: >\n        {DESCRIPTIONS[item.kind].format(base=item.base)}\n"
                f"    version: {self.version_text(version)}\n\n\n{body}")

    def version_text(self, version) -> str:
        """info.version écrit dans un fichier créé : celle du fichier source (ex. 1.14.0), sinon x.y.0."""
        text = self.info_version or f"{version[0]}.{version[1]}.0"
        return f"'{text}'" if re.fullmatch(r"\d+(\.\d+)?", text) else text  # 1.14 resterait sinon un nombre


def split_components(components: dict, out: Path, source: str, source_ref: str | None = None,
                     api: str | None = None, info_version: str | None = None) -> dict[str, dict[str, tuple[str, tuple[int, int]]]]:
    """Prépare, versionne et écrit des composants. Renvoie {section: {nom d'origine: (nom final, version)}}."""
    items, names = prepare(components, source, source_ref)
    result = Versioner(items, out, source, api, info_version).run()
    return {section: {original: result[new] for original, new in mapping.items()} for section, mapping in names.items()}


def follow_block_version(version: str) -> None:
    """La dernière version de l'API BLOCK_API prend la version du fichier en bloc, si elle est plus grande
    (jamais en arrière : après un sync, rdv-actor peut déjà être en 1.14.1 alors que le bloc est en 1.14.0)."""
    folders = sorted((int(d.name[1:]), d / f"{BLOCK_API}.yaml") for d in (APIS_DIR / BLOCK_API).glob("v*")
                     if d.name[1:].isdigit() and (d / f"{BLOCK_API}.yaml").is_file())
    if not folders:
        print(f"  · API {BLOCK_API} introuvable dans {APIS_DIR} : sa version n'est pas mise à jour (BLOCK_API)")
        return
    api = folders[-1][1]
    text = api.read_text(encoding="utf-8")
    current, _ = info_version(text)
    label = f"{BLOCK_API}/v{folders[-1][0]}/{api.name}"
    if current is None:
        print(f"  ⚠ {label} : info.version introuvable, à mettre à {version} à la main")
    elif version_tuple(version) > version_tuple(current):
        api.write_text(with_info_version(text, version), encoding="utf-8")
        print(f"  ↑ {label} : info.version {current} -> {version} (version de {SOURCE_FILE.name})")
    else:
        print(f"  · {label} : info.version {current} gardée ({SOURCE_FILE.name} est en {version})")


def main() -> None:
    try:
        if not SOURCE_FILE.is_file():
            raise ValueError(f"Fichier de composants introuvable : {SOURCE_FILE.resolve()} (adaptez SOURCE_FILE)")
        doc = load(SOURCE_FILE)
        if not isinstance(doc, dict) or doc.get("paths"):
            raise ValueError(f"{SOURCE_FILE} : fichier de composants attendu (openapi + info + components, sans paths)")
        components = doc.get("components") or {}
        if not any(components.get(section) for section in SECTIONS):
            raise ValueError(f"{SOURCE_FILE} : aucun composant à découper ({', '.join(SECTIONS)})")
        print(f"✔ {SOURCE_FILE} -> {SCHEMAS_DIR}/")
        source_ref = relpath(SOURCE_FILE, SCHEMAS_DIR / "X")  # vu depuis un dossier de composant
        version = (doc.get("info") or {}).get("version")
        version = str(version).strip() if version not in (None, "") else None
        split_components(components, SCHEMAS_DIR, SOURCE_FILE.name, source_ref, info_version=version)
        if version:
            follow_block_version(version)
        others = sorted(set(components) - set(SECTIONS))
        if others:
            print(f"  · non découpé, laissé dans {SOURCE_FILE.name} : {', '.join(others)}")
    except (ValueError, OSError) as error:
        sys.exit(f"✖ {error}")


if __name__ == "__main__":
    main()
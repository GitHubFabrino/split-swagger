#!/usr/bin/env python3
"""
v3 — Découpe le fichier de composants en bloc (SOURCE_FILE) dans le dossier unique SCHEMAS_DIR :
un dossier par composant (schemas, parameters, responses, securitySchemes), une version par fichier.

    python split_openapi_v3.py

Versions : identique -> réutilisé ; modifié -> mineure (1.3 -> 1.4) ; cassant -> majeure (1.3 -> 2.0),
annoncée par un message, sans confirmation ; une dépendance passée en majeure entraîne ses utilisateurs.
Les autres sections du fichier en bloc (examples, headers…) y restent ; les références y pointent.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common_v3 import (  # noqa: E402
    LOCAL, SCHEMAS_DIR, SECTIONS, SOURCE_FILE, SPLIT_REF, all_refs, api_name, breaking_changes, dump,
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
      identique à une version existante            -> réutilisée (dernière de sa majeure) ;
      modifié                                      -> mineure suivante de la dernière version (1.3 -> 1.4) ;
      modifié de façon cassante, ou une dépendance -> majeure suivante (1.3 -> 2.0), sans confirmation ;
      passée en majeure
      nouveau                                      -> 1.0.
    api (extraction) : un composant dont le nom existe déjà, créé depuis une autre source, est comparé
    détail par détail : identique -> réutilisé ; différent -> renommé avec le nom de l'API."""

    def __init__(self, items: dict[str, Item], out: Path, source: str, api: str | None = None):
        self.items, self.out, self.source, self.api = items, out, source, api
        self.final: dict[str, str] = {}               # renommage avec le nom de l'API
        self.chosen: dict[str, tuple[int, int]] = {}
        self.majors: set[str] = set()                 # passés en majeure
        self.writes: list[tuple[str, tuple, dict, str | None]] = []
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
            disk, renamed = {self.fn(n) for n in group}, False
            for name in group:
                if name in self.final or not self.versions(name):
                    continue
                latest = self.versions(name)[-1]
                existing, draft, source = self.read(name, latest), self.content(name, members), self.source_of(name)
                label = file_name(name, latest)[:-5]
                if shape(existing, disk) == shape(draft, disk):
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

    def decide(self, group: list[str]) -> None:
        if self.api:
            self.check_names(group)
        members = set(group)
        disk = {self.fn(n) for n in group}
        draft = {n: self.content(n, members) for n in group}

        # 1. Tous identiques à une version existante : réutilisés.
        matches = {}
        for n in group:
            versions = self.versions(n)
            match = next((v for v in reversed(versions) if shape(self.read(n, v), disk) == shape(draft[n], disk)), None)
            if match:
                matches[n] = max(v for v in versions if v[0] == match[0])
        if len(matches) == len(group):
            for n in group:
                self.chosen[n] = matches[n]
                self.writes.append((self.fn(n), matches[n], self.read(n, matches[n]), None))
            return

        # 2. Nouvelle version pour tout le groupe : majeure si cassant ou si une dépendance l'est devenue.
        latest = {n: self.versions(n)[-1] for n in group if self.versions(n)}
        breaking = {n: b for n in latest if (b := breaking_changes(self.read(n, latest[n]), draft[n]))}
        cascade = sorted(self.fn(d) for n in group for d in self.deps(n) - members if d in self.majors)
        is_major = bool(breaking or cascade)
        for n in group:
            if n not in latest:
                self.chosen[n] = (1, 0)
            elif is_major:
                self.chosen[n] = (latest[n][0] + 1, 0)
                self.majors.add(n)
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
                    moved = sorted(d for d, v in ref_versions(content).items() if ref_versions(old).get(d) != v)
                    why = f"cascade : {', '.join(moved)}" if moved else "cascade dans le cycle"
                else:
                    why = "contenu modifié"
                reason = f"{before} -> {version[0]}.{version[1]}, {why}" + (f" ({cycle})" if cycle else "")
                if is_major:
                    self.warnings.append(
                        f"{self.fn(n)} passé en version majeure {version[0]}.0"
                        + (" (changement cassant)" if n in breaking else f" (dépendance passée en majeure : "
                           f"{', '.join(cascade) or cycle})")
                        + f". Les API restent sur la {latest[n][0]}.x : passez-les à la {version[0]}.0 à la main "
                          f"quand elles sont prêtes.")
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
        reused = [file_name(disk, version)[:-5] for disk, version, _, reason in self.writes if not reason]
        if reused:
            print(f"  = réutilisés ({len(reused)}) : " + ", ".join(reused[:30])
                  + (f", … (+{len(reused) - 30})" if len(reused) > 30 else ""))
        for warning in self.warnings:
            print(f"  ⚠ {warning}")
        return {name: (self.fn(name), self.chosen[name]) for name in self.items}

    def render(self, name: str, disk: str, version, content: dict) -> str:
        item = self.items[name]
        banner = "#" * 62
        title = ("#" + f"{SECTION_TITLE[item.section]} file information".center(60)).rstrip()
        body = dump({"components": {item.section: {disk: content}}}, indent=4)
        body = re.sub(r"^(\s*(?:-\s+)?\$ref: )(\.\./\S+)$", r"\1'\2'", body, flags=re.M)
        return (f"# created on: {date.today():%d/%m/%Y} split from {self.source}\n\n{banner}\n{title}\n{banner}\n\n"
                f"info:\n    title: {disk}\n    description: >\n        {DESCRIPTIONS[item.kind].format(base=item.base)}\n"
                f"    version: {version[0]}.{version[1]}.0\n\n\n{body}")


def split_components(components: dict, out: Path, source: str, source_ref: str | None = None,
                     api: str | None = None) -> dict[str, dict[str, tuple[str, tuple[int, int]]]]:
    """Prépare, versionne et écrit des composants. Renvoie {section: {nom d'origine: (nom final, version)}}."""
    items, names = prepare(components, source, source_ref)
    result = Versioner(items, out, source, api).run()
    return {section: {original: result[new] for original, new in mapping.items()} for section, mapping in names.items()}


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
        split_components(components, SCHEMAS_DIR, SOURCE_FILE.name, source_ref)
        others = sorted(set(components) - set(SECTIONS))
        if others:
            print(f"  · non découpé, laissé dans {SOURCE_FILE.name} : {', '.join(others)}")
    except (ValueError, OSError) as error:
        sys.exit(f"✖ {error}")


if __name__ == "__main__":
    main()
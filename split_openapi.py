#!/usr/bin/env python3
"""
Découpe un fichier de schémas OpenAPI (components.schemas, sans paths) en un fichier
par schéma et par version :

    <dossier des schémas>/<Nom>/<Nom>_V<x>.<y>.yaml

Renommage (voir openapi_common.schema_naming) :
    préfixe retiré (S_, E_, A_ …), séparateurs supprimés, PascalCase, puis suffixe :
    S_ActorCompany -> ActorCompanyObject, E_ActorComposite_Input -> ActorCompositeInput,
    E_Actor_Output -> ActorOutput. Deux schémas qui donneraient le même nom : erreur.

Chaque fichier contient un en-tête (date, fichier source), un bloc info (title = nom,
description selon Object / Input / Output, version x.y.0) et components.schemas.<Nom>.

Usage :
    python3 split_openapi.py [schemas.yaml] [dossier-des-schémas] [--major Nom,Nom] [--dry-run]

    Sans argument : SOURCE_FILE et SCHEMAS_DIR de openapi_common.py (configuration du projet).
    Le dossier des schémas est créé automatiquement.

Versionnement automatique :
    schéma nouveau                          -> _v1.0
    identique à une version existante       -> réutilisée (la dernière de sa majeure)
    modifié                                 -> version mineure suivante, dans la majeure compatible
    --major Nom (changement cassant)        -> version majeure suivante (_v1.1 -> _v2.0)
    une dépendance change de version        -> nouvelle version aussi (cascade)
    schémas en cycle (A -> B -> A)          -> versionnés ensemble : si l'un change, tous changent

Les références internes (#/components/schemas/Y), y compris dans discriminator.mapping,
deviennent ../NouveauNom/NouveauNom_V<x.y>.yaml#/components/schemas/NouveauNom.
Les extensions x-* sont recopiées telles quelles.

Pour repointer ensuite les contrats d'API vers les dernières versions : sync_schemas.py.

Dépendances : PyYAML (pip install pyyaml) et openapi_common.py, à garder à côté de ce script.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from openapi_common import (  # noqa: E402
    DESCRIPTIONS, LOCAL, SCHEMAS_DIR, SOURCE_FILE, breaking_changes, canon, dump, load, map_refs, other_refs, rename_map, schema_naming, schema_refs,
    shape, without_sources)


# ================================================================ versions sur le disque

@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


def file_name(name: str, version) -> str:
    return f"{name}_V{version}.yaml"


def fragment(name: str) -> str:
    """Le schéma est dans components.schemas.<Nom> de son fichier : une $ref doit y pointer,
    sinon elle désigne tout le document (info compris)."""
    return f"#/components/schemas/{name}"


def schema_file(schemas_dir: Path, name: str, version: Version) -> Path:
    return schemas_dir / name / file_name(name, version)


def existing_versions(schemas_dir: Path, name: str) -> list[Version]:
    """Versions présentes dans <dossier des schémas>/<Nom>/, de la plus ancienne à la plus récente."""
    folder = schemas_dir / name
    if not folder.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(name)}_V(\d+)\.(\d+)\.yaml$")
    matches = (pattern.match(f.name) for f in folder.iterdir())
    return sorted(Version(int(m[1]), int(m[2])) for m in matches if m)


# ================================================================ analyse des schémas

def dependency_majors(content, ignore: set[str]) -> set[tuple[str, str]]:
    """Dépendances d'un contenu avec leur majeure : {("Order", "1"), ...}, hors noms ignorés."""
    found = re.findall(r"\.\./([^/\"]+)/\1_[vV](\d+)\.\d+\.yaml", canon(without_sources(content)))
    return {(name, major) for name, major in found if name not in ignore}


def groups_in_dependency_order(graph: dict[str, set[str]]) -> list[list[str]]:
    """Composantes fortement connexes (algorithme de Tarjan), dépendances d'abord.
    Un schéma seul forme un groupe d'un élément ; des schémas en cycle (A -> B -> A, ou A -> A)
    forment un seul groupe. Version itérative : la récursion de Python s'arrête vers 1 000 niveaux,
    trop peu pour de longues chaînes de dépendances."""
    edges = {name: sorted(deps) for name, deps in graph.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[list[str]] = []

    for root in edges:
        if root in index:
            continue
        work = [(root, 0)]  # (schéma, prochaine dépendance à examiner) : remplace la pile d'appels
        while work:
            node, next_dep = work.pop()
            if next_dep == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            descended = False
            for position in range(next_dep, len(edges[node])):
                dep = edges[node][position]
                if dep not in index:
                    work.append((node, position + 1))  # reprendre ici après la dépendance
                    work.append((dep, 0))
                    descended = True
                    break
                if dep in on_stack:
                    low[node] = min(low[node], index[dep])
            if descended:
                continue
            if low[node] == index[node]:  # node est la racine d'un groupe : on le dépile
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                groups.append(sorted(group))
            if work:  # retour au parent
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return groups


# ================================================================ lecture et contrôles

# Référence vers un fichier de schéma déjà découpé : …/Nom/Nom_V1.2.yaml(#/components/schemas/Nom)
_SCHEMA_FILE_REF = re.compile(
    r"(?:^|/)(?P<name>[^/#]+)/(?P=name)_[vV](?P<version>\d+\.\d+)\.yaml(?:#/components/schemas/(?P=name))?$")


def _to_sibling_refs(node):
    """Les $ref vers un fichier de schéma déjà découpé deviennent ../Nom/Nom_Vx.y.yaml#/components/schemas/Nom :
    chemin valable depuis n'importe quel dossier de schéma (ils sont tous voisins)."""
    if isinstance(node, list):
        return [_to_sibling_refs(item) for item in node]
    if not isinstance(node, dict):
        return node
    copy = {}
    for key, value in node.items():
        m = _SCHEMA_FILE_REF.search(value) if key == "$ref" and isinstance(value, str) else None
        copy[key] = (f"../{m['name']}/{file_name(m['name'], m['version'])}{fragment(m['name'])}" if m
                     else _to_sibling_refs(value))
    return copy


def prepare_schemas(schemas: dict, where: str) -> tuple[dict, dict]:
    """Contrôle et renomme des schémas : ({NouveauNom: schéma}, {NouveauNom: (base, type)}).
    Les références internes visent les nouveaux noms ; celles vers un fichier de schéma déjà découpé sont
    acceptées. Base et type viennent du nom d'origine (S_ActorCompany -> "ActorCompany", "Object") :
    ils servent à la description du fichier."""
    names = set(schemas)
    external = [ref for ref in other_refs(schemas, names) if not _SCHEMA_FILE_REF.search(ref)]
    if external:
        raise ValueError(f"{where} : {len(external)} référence(s) ne visent ni un schéma du fichier ni un fichier "
                         f"de schéma découpé (ex. {external[0]}).")
    for name, schema in schemas.items():
        missing = sorted(schema_refs(schema, names) - names)
        if missing:
            raise ValueError(f"{where} : {name} référence un schéma absent du fichier : {missing[0]}")

    new_names = rename_map(schemas)
    renamed = {new_names[name]: _to_sibling_refs(without_sources(
                   map_refs(schema, names, lambda dep: LOCAL + new_names[dep])))
               for name, schema in schemas.items()}
    origins = {new_names[name]: schema_naming(name)[1:] for name in schemas}
    return renamed, origins


def read_schemas(source: Path) -> tuple[dict, dict]:
    """Schémas d'un fichier de schémas en bloc, contrôlés et renommés (voir prepare_schemas)."""
    if not source.is_file():
        raise ValueError(f"Fichier de schémas introuvable : {source.resolve()} "
                         f"(lancez le script depuis la racine du projet, ou adaptez SOURCE_FILE dans openapi_common.py)")
    doc = load(source)
    if not isinstance(doc, dict):
        raise ValueError(f"{source} n'est pas un fichier YAML OpenAPI")
    if doc.get("paths") or doc.get("webhooks"):
        raise ValueError(f"{source} contient des paths : ce script attend un fichier de schémas seulement "
                         f"(openapi + info + components.schemas). Pour une API, utilisez extract_api_schemas.py.")
    schemas = (doc.get("components") or {}).get("schemas")
    if not schemas:
        raise ValueError(f"{source} : aucun schéma dans components.schemas")
    return prepare_schemas(schemas, str(source))


# ================================================================ décisions de version

class Splitter:
    """Décide la version de chaque schéma, sans rien écrire (les écritures sont dans self.writes)."""

    def __init__(self, schemas: dict, out: Path, forced_major: set[str]):
        self.schemas = schemas
        self.out = out
        self.forced = forced_major
        self.names = set(schemas)
        self.chosen: dict[str, Version] = {}          # version retenue pour chaque schéma traité
        self.renewed: set[str] = set()                # schémas qui reçoivent une nouvelle version
        self.major_bumped: set[str] = set()           # schémas passés à une nouvelle majeure
        self.writes: list[tuple[str, Version, dict, str | None]] = []  # (Nom, version, contenu, raison ; None = réutilisé)
        self.warnings: list[str] = []
        self.breaking: dict[str, list[str]] = {}      # nouvelle version mineure qui semble cassante : détails
        self._versions: dict[str, list[Version]] = {}
        self._files: dict[tuple[str, Version], dict] = {}

    # -- accès au disque, mis en cache (rien n'est écrit pendant les décisions)
    def versions(self, name: str) -> list[Version]:
        if name not in self._versions:
            self._versions[name] = existing_versions(self.out, name)
        return self._versions[name]

    def read(self, name: str, version: Version) -> dict:
        key = (name, version)
        if key not in self._files:
            doc = load(schema_file(self.out, name, version))
            self._files[key] = doc["components"]["schemas"][name]
        return self._files[key]

    def latest_in_major(self, name: str, major: int) -> Version:
        return [v for v in self.versions(name) if v.major == major][-1]

    # -- contenu tel qu'il serait écrit
    def deps(self, name: str) -> set[str]:
        return schema_refs(self.schemas[name], self.names)

    def content(self, name: str, undecided: set[str] = frozenset()) -> dict:
        """Le schéma, ses références pointant vers les versions choisies.
        undecided : schémas dont la version n'est pas encore choisie (provisoirement v0.0)."""
        def to_file(dep: str) -> str:
            version = "0.0" if dep in undecided else self.chosen[dep]
            return f"../{dep}/{file_name(dep, version)}{fragment(dep)}"
        return without_sources(map_refs(self.schemas[name], self.names, to_file))

    # -- décision pour un groupe : un schéma seul, ou des schémas en cycle
    def decide(self, group: list[str]) -> None:
        members = set(group)
        cyclic = len(group) > 1 or group[0] in self.deps(group[0])
        draft = {name: self.content(name, undecided=members) for name in group}

        # 1. Tous identiques à une version existante : on les réutilise.
        matches = {name: self.find_identical(name, draft[name], members) for name in group}
        if all(matches.values()):
            for name in group:
                self.reuse(name, matches[name])
            return

        # 2. Sinon tous reçoivent une nouvelle version (dans un cycle, ils se référencent mutuellement).
        is_major = bool(members & self.forced) or any(
            dep in self.major_bumped for name in group for dep in self.deps(name) - members)
        existing = [name for name in group if self.versions(name)]
        previous = self.base_versions(existing, draft, members, is_major)
        for name in group:
            if name not in previous:
                self.chosen[name] = Version(1, 0)
            elif is_major:
                self.chosen[name] = Version(previous[name].major + 1, 0)
                self.major_bumped.add(name)
            else:
                self.chosen[name] = Version(previous[name].major, previous[name].minor + 1)
        self.renewed |= members

        # 3. Contenu définitif, raison et avertissement.
        label = " ↔ ".join(group)
        for name in group:
            content = self.content(name)
            if name not in previous:
                reason = "nouveau" + (f", cycle : {label}" if cyclic else "")
            else:
                old = self.read(name, previous[name])
                reason = f"{previous[name]} -> {self.chosen[name]}, {self.why(name, old, content, members, is_major, cyclic)}"
                if cyclic:
                    reason += f" ({label})"
                if not is_major:
                    self.warn_if_breaking(name, old, content)
            self.writes.append((name, self.chosen[name], content, reason))

    def find_identical(self, name: str, draft: dict, members: set[str]) -> Version | None:
        """Version existante identique au brouillon (versions des membres du groupe ignorées)."""
        if name in self.forced:
            return None
        target = shape(draft, only=members)
        return next((v for v in reversed(self.versions(name)) if shape(self.read(name, v), only=members) == target), None)

    def reuse(self, name: str, match: Version) -> None:
        """Réutilise la dernière version de la majeure de match (règle « toujours la dernière »)."""
        target = self.latest_in_major(name, match.major)
        self.chosen[name] = target
        self.writes.append((name, target, self.read(name, target), None))
        if target != match:
            self.warnings.append(f"{name} : identique à l'ancienne v{match} -> v{target} (dernière de la majeure) "
                                 f"conservée. Le fichier contenait une copie périmée de ce schéma.")

    def base_versions(self, group: list[str], draft: dict, members: set[str], is_major: bool) -> dict[str, Version]:
        """Version de départ de chaque membre pour sa nouvelle version.
        Majeure : la plus haute existante. Mineure : la dernière version d'une ligne majeure
        commune à tout le groupe (un cycle reste dans une seule ligne) ; voir candidate_majors."""
        if is_major:
            return {name: self.versions(name)[-1] for name in group}
        candidates = {name: self.candidate_majors(name, draft[name], members) for name in group}
        common = [m for m in candidates[group[0]] if all(m in c for c in candidates.values())] if group else []
        return {name: self.latest_in_major(name, common[0] if common else candidates[name][0]) for name in group}

    def candidate_majors(self, name: str, draft: dict, members: set[str]) -> list[int]:
        """Lignes majeures où le nouveau contenu peut prendre place, de la meilleure à la moins bonne :
        celles dont la dernière version a les mêmes majeures de dépendances et vis-à-vis de laquelle
        le changement n'est pas cassant, en commençant par la plus haute.
        Ex. : on fait évoluer Order 1.1 alors que Order_v2.0 existe -> ligne 1, donc Order_v1.2."""
        majors = sorted({v.major for v in self.versions(name)}, reverse=True)
        latest = {m: self.read(name, self.latest_in_major(name, m)) for m in majors}
        wanted = dependency_majors(draft, ignore=members)
        same_deps = [m for m in majors if dependency_majors(latest[m], ignore=members) == wanted] or majors
        compatible = [m for m in same_deps if not breaking_changes(latest[m], draft)]
        return compatible + [m for m in same_deps if m not in compatible]

    def why(self, name: str, old: dict, new: dict, members: set[str], is_major: bool, cyclic: bool) -> str:
        if name in self.forced:
            return "changement cassant (--major)"
        if shape(old) == shape(new):  # seules les versions des références ont changé
            changed = sorted((self.deps(name) - members) & self.renewed)
            cause = f"cascade : {', '.join(changed)}" if changed else "cascade dans le cycle"
            return cause + (" (majeure)" if is_major else "")
        if is_major:
            return "contenu modifié + " + ("cycle passé en majeure" if cyclic else "dépendance passée en majeure")
        return "contenu modifié"

    def warn_if_breaking(self, name: str, old: dict, new: dict) -> None:
        breaking = breaking_changes(old, new)
        if breaking:
            self.breaking[name] = breaking
            self.warnings.append(f"{name}_V{self.chosen[name]} semble cassant ({' ; '.join(breaking)}). "
                                 f"Si c'est voulu, relancez avec --major {name} (et --dry-run pour vérifier).")

    def run(self) -> None:
        graph = {name: self.deps(name) for name in self.schemas}
        for group in groups_in_dependency_order(graph):
            self.decide(group)


# ================================================================ écriture et rapport

BANNER = "#" * 62
BANNER_TITLE = ("#" + "Schema file information".center(60)).rstrip()


_SPLIT_FROM = re.compile(r"^# created on: \S+ split from (?P<source>.+)$", re.M)


def _origin_note(out: Path, splitter: "Splitter", name: str, source_name: str) -> str:
    """« (existant créé depuis X) » quand la dernière version d'un schéma vient d'un autre fichier."""
    versions = splitter.versions(name)
    if not versions:
        return ""
    origin = _SPLIT_FROM.search(schema_file(out, name, versions[-1]).read_text(encoding="utf-8"))
    source = origin["source"].strip() if origin else ""
    return f" (existant créé depuis {source})" if source and source != source_name else ""


def render(name: str, version: Version, content: dict, source_name: str, origin: tuple[str, str]) -> str:
    """Texte d'un fichier de schéma : en-tête, info, components.schemas.<Nom>."""
    base, kind = origin
    header = (f"# created on: {date.today():%d/%m/%Y} split from {source_name}\n\n"
              f"{BANNER}\n{BANNER_TITLE}\n{BANNER}\n\n")
    info = (f"info:\n"
            f"    title: {name}\n"
            f"    description: >\n"
            f"        {DESCRIPTIONS[kind].format(base=base)}\n"
            f"    version: {version}.0\n\n\n")
    body = dump({"components": {"schemas": {name: content}}}, indent=4)
    body = re.sub(r"^(\s*(?:-\s+)?\$ref: )(\.\./\S+)$", r"\1'\2'", body, flags=re.M)  # $ref: '../X/X_V1.0.yaml#/…'
    return header + info + body


def split_schemas(schemas: dict, origins: dict, out: Path, forced_major: set[str], dry_run: bool,
                  source_name: str, title: str, strict: bool = False) -> list[tuple[str, Version, str]]:
    """Versionne et écrit des schémas déjà préparés (prepare_schemas) dans le dossier des schémas.
    Affiche le rapport sous le titre donné. Renvoie les fichiers créés : [(Nom, version, raison)].
    strict : une nouvelle version mineure qui semble cassante bloque tout (ValueError, rien n'est écrit) ;
    utilisé par extract_api_schemas.py, qui synchronise aussitôt les API."""
    forced = {schema_naming(name)[0] for name in forced_major}  # --major accepte l'ancien ou le nouveau nom
    unknown = sorted(name for name in forced_major if schema_naming(name)[0] not in schemas)
    if unknown:
        raise ValueError(f"--major : schéma(s) absent(s) : {', '.join(unknown)}")

    splitter = Splitter(schemas, out, forced)
    splitter.run()
    if strict and splitter.breaking:
        details = "\n".join(
            f"      - {name} : {' ; '.join(items)}" + _origin_note(out, splitter, name, source_name)
            for name, items in splitter.breaking.items())
        names = ",".join(splitter.breaking)
        raise ValueError(
            f"changement cassant sans --major, rien n'a été écrit pour cette API :\n{details}\n"
            f"      → si c'est un autre schéma, renommez-le dans {source_name} ;\n"
            f"      → si c'est bien une évolution cassante, relancez avec --major {names} "
            f"(les autres API restent sur leur version actuelle).")
    created = [(name, version, content, reason) for name, version, content, reason in splitter.writes if reason]

    # Un schéma du même nom, créé depuis un autre fichier, reçoit une nouvelle version : elle servira aussi
    # aux API qui l'utilisaient. On le signale, car ce n'est peut-être pas le même schéma.
    warnings = list(splitter.warnings)
    for name, version, _, reason in created:
        previous = [v for v in splitter.versions(name) if v != version]
        if previous:
            text = schema_file(out, name, previous[-1]).read_text(encoding="utf-8")
            origin = _SPLIT_FROM.search(text)
            if origin and origin["source"].strip() != source_name:
                if version.minor == 0:  # nouvelle majeure : les API restent sur leur majeure actuelle
                    warnings.append(f"{name} existait déjà (créé depuis {origin['source'].strip()}) : nouvelle "
                                    f"version majeure v{version} pour {source_name} ; les API qui utilisent "
                                    f"la v{previous[-1].major}.x n'en reçoivent pas les changements.")
                else:
                    warnings.append(f"{name} existait déjà (créé depuis {origin['source'].strip()}) avec un contenu "
                                    f"différent : la v{version} venant de {source_name} servira aussi aux API qui "
                                    f"l'utilisent. Si ce n'est pas le même schéma, renommez-le dans {source_name}.")

    if not dry_run:
        for name, version, content, _ in created:  # une version réutilisée n'est jamais réécrite
            path = schema_file(out, name, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(name, version, content, source_name, origins[name]), encoding="utf-8")

    reused = [file_name(name, version)[:-5] for name, version, _, reason in splitter.writes if not reason]
    print(f"{'[simulation] ' if dry_run else ''}{title}")
    for name, version, _, reason in created:
        print(f"  + {name}/{file_name(name, version)}  ({reason})")
    if reused:
        shown = ", ".join(reused[:30]) + (f", … (+{len(reused) - 30})" if len(reused) > 30 else "")
        print(f"  = réutilisés ({len(reused)}) : {shown}")
    for warning in warnings:
        print(f"  ⚠ {warning}")
    return [(name, version, reason) for name, version, _, reason in created]


def split(source: Path, out: Path, forced_major: set[str], dry_run: bool) -> None:
    schemas, origins = read_schemas(source)
    created = split_schemas(schemas, origins, out, forced_major, dry_run, source.name, f"✔ {source} -> {out}/")
    if any(not reason.startswith("nouveau") for *_, reason in created):
        print("  → Lancez sync_schemas.py pour repointer les contrats vers ces nouvelles versions.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Découpe un fichier de schémas OpenAPI en <dossier des schémas>/<Nom>/<Nom>_V<x.y>.yaml.")
    parser.add_argument("source", type=Path, nargs="?", default=SOURCE_FILE,
                        help=f"fichier de schémas en bloc (défaut : {SOURCE_FILE})")
    parser.add_argument("out", type=Path, nargs="?", default=SCHEMAS_DIR,
                        help=f"dossier des schémas, créé si besoin (défaut : {SCHEMAS_DIR})")
    parser.add_argument("--major", default="", help="schémas ayant un changement cassant, séparés par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="affiche ce qui serait fait sans rien écrire")
    args = parser.parse_args()
    try:
        split(args.source, args.out, {n for n in args.major.split(",") if n}, args.dry_run)
    except (ValueError, OSError, yaml.YAMLError) as error:
        print(f"✖ {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
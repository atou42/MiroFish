"""
Compile raw lore materials into a runnable world-mode simulation bundle.

The compiler reuses the existing world-mode generators instead of inventing a
parallel pipeline. It normalizes source files into entity seeds, bootstraps a
local project/simulation state, and writes a ready-to-run simulation directory.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config
from ..models.project import ProjectManager, ProjectStatus
from ..models.simulation_mode import SimulationMode
from ..utils.logger import get_logger
from .simulation_manager import SimulationManager, SimulationState, SimulationStatus
from .world_config_generator import WorldConfigGenerator
from .world_preset_registry import WorldPresetRegistry
from .world_profile_generator import WorldProfileGenerator
from .zep_entity_reader import EntityNode

logger = get_logger("mirofish.world_pack_compiler")

TEXT_EXTENSIONS = {
    ".json",
    ".jsonl",
    ".markdown",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_NAMES = {".ds_store"}
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv"}
ENTITY_EXTRACTION_SKIP_FILENAMES = {"sources.json", "run.json", "images.json", "asset.json"}
ACTOR_ENTITY_TYPES = {
    "character",
    "faction",
    "organization",
    "institution",
    "person",
    "crew",
    "clan",
    "force",
    "army",
    "government",
    "kingdom",
    "nation",
}
SECTION_KEY_PATTERN = re.compile(r"^\s*-\s*([^:：]+)\s*[:：]\s*(.+?)\s*$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class WorldPackSource:
    rel_path: str
    abs_path: str
    kind: str
    size_bytes: int
    char_count: int
    line_count: int
    excerpt: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "abs_path": self.abs_path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "char_count": self.char_count,
            "line_count": self.line_count,
            "excerpt": self.excerpt,
        }


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(value or "").strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "world-pack"


def _shorten(text: str, limit: int = 260) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _stable_uuid(namespace: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{name}"))


def _coerce_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _coerce_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


class WorldPackCompiler:
    def __init__(self) -> None:
        self._profile_generator = WorldProfileGenerator()
        self._simulation_manager = SimulationManager()

    def compile(
        self,
        *,
        source_dir: str,
        simulation_requirement: str = "",
        simulation_id: str = "",
        project_id: str = "",
        graph_id: str = "",
        world_preset_id: Optional[str] = None,
        pack_id: str = "",
        pack_title: str = "",
        use_llm_for_profiles: bool = True,
        use_llm_for_config: bool = False,
        profile_parallel_count: int = 3,
    ) -> Dict[str, Any]:
        source_root = Path(source_dir).expanduser().resolve()
        if not source_root.is_dir():
            raise ValueError(f"source_dir not found: {source_root}")

        pack_title = pack_title.strip() or source_root.name
        resolved_pack_id = pack_id.strip() or _slugify(pack_title)
        resolved_simulation_id = simulation_id.strip() or f"sim_pack_{uuid.uuid4().hex[:12]}"
        sim_dir = Path(Config.UPLOAD_FOLDER).resolve() / "simulations" / resolved_simulation_id
        pack_dir = sim_dir / "world_pack"
        os.makedirs(pack_dir, exist_ok=True)

        source_records, source_texts = self._collect_sources(source_root)
        if not source_records:
            raise ValueError(f"no supported text sources found under {source_root}")

        document_text = self._build_document_text(source_texts)
        if not document_text.strip():
            raise ValueError(f"no readable content found under {source_root}")

        all_entities = self._extract_entities(
            source_root=source_root,
            pack_namespace=resolved_pack_id,
            source_texts=source_texts,
        )
        actor_entities = self._select_actor_entities(all_entities)
        if not actor_entities:
            actor_entities = list(all_entities[: min(12, len(all_entities))])

        world_preset = WorldPresetRegistry.get_preset(world_preset_id)
        profiles = self._profile_generator.generate_profiles_from_entities(
            entities=actor_entities,
            use_llm=use_llm_for_profiles,
            parallel_count=max(1, profile_parallel_count),
            world_preset=world_preset,
        )

        project = self._bootstrap_project(
            project_id=project_id,
            project_name=pack_title,
            graph_id=graph_id,
            simulation_requirement=simulation_requirement,
            document_text=document_text,
            source_dir=str(source_root),
        )
        state = self._bootstrap_simulation_state(
            simulation_id=resolved_simulation_id,
            project_id=project.project_id,
            graph_id=project.graph_id or graph_id or f"world_pack_{resolved_pack_id}",
        )

        requirement = (
            simulation_requirement.strip()
            or project.simulation_requirement
            or self._default_requirement(pack_title)
        )
        config_generator = WorldConfigGenerator()
        if not use_llm_for_config:
            config_generator._llm_checked = True
            config_generator._llm = None
        world_config = config_generator.generate_config(
            simulation_id=state.simulation_id,
            project_id=state.project_id,
            graph_id=state.graph_id,
            simulation_requirement=requirement,
            document_text=document_text,
            entities=all_entities,
            profiles=profiles,
            world_preset=world_preset,
        )
        config_payload = world_config.to_dict()
        config_payload["world_pack"] = {
            "pack_id": resolved_pack_id,
            "title": pack_title,
            "source_dir": str(source_root),
            "compiled_at": datetime.now().isoformat(),
            "source_count": len(source_records),
            "entity_count": len(all_entities),
            "actor_entity_count": len(actor_entities),
            "profile_count": len(profiles),
        }

        config_path = sim_dir / "simulation_config.json"
        profiles_path = sim_dir / "world_profiles.json"
        entities_path = pack_dir / "compiled_entities.json"
        actors_path = pack_dir / "compiled_actor_entities.json"
        sources_path = pack_dir / "sources.json"
        digest_path = pack_dir / "source_digest.md"
        manifest_path = pack_dir / "manifest.json"

        os.makedirs(sim_dir, exist_ok=True)
        profiles_path.write_text(
            json.dumps([profile.to_dict() for profile in profiles], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        entities_path.write_text(
            json.dumps([entity.to_dict() for entity in all_entities], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        actors_path.write_text(
            json.dumps([entity.to_dict() for entity in actor_entities], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        sources_path.write_text(
            json.dumps([record.to_dict() for record in source_records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        digest_path.write_text(
            self._build_digest_markdown(
                pack_title=pack_title,
                source_dir=str(source_root),
                source_records=source_records,
                all_entities=all_entities,
                actor_entities=actor_entities,
            ),
            encoding="utf-8",
        )
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self._hydrate_runtime_metadata(
            state=state,
            world_preset_id=world_preset.preset_id,
            entity_count=len(all_entities),
            actor_entity_count=len(actor_entities),
            profile_count=len(profiles),
            entity_types=sorted({entity.get_entity_type() or "Unknown" for entity in all_entities}),
            runtime_config=config_payload.get("runtime_config") or {},
            pack_id=resolved_pack_id,
            pack_title=pack_title,
        )

        manifest = {
            "schema_version": 1,
            "pack_id": resolved_pack_id,
            "title": pack_title,
            "compiled_at": datetime.now().isoformat(),
            "source_dir": str(source_root),
            "simulation_id": state.simulation_id,
            "project_id": state.project_id,
            "graph_id": state.graph_id,
            "world_preset_id": world_preset.preset_id,
            "simulation_requirement": requirement,
            "source_count": len(source_records),
            "entity_count": len(all_entities),
            "actor_entity_count": len(actor_entities),
            "profile_count": len(profiles),
            "output_files": {
                "simulation_dir": str(sim_dir),
                "config_path": str(config_path),
                "profiles_path": str(profiles_path),
                "compiled_entities_path": str(entities_path),
                "compiled_actor_entities_path": str(actors_path),
                "sources_path": str(sources_path),
                "source_digest_path": str(digest_path),
            },
            "source_files": [record.to_dict() for record in source_records],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path)
        return manifest

    def _collect_sources(self, source_root: Path) -> Tuple[List[WorldPackSource], List[Tuple[str, str, str]]]:
        records: List[WorldPackSource] = []
        source_texts: List[Tuple[str, str, str]] = []
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name.lower() in SKIP_NAMES:
                continue
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue

            rel_path = path.relative_to(source_root).as_posix()
            text = self._read_text_file(path)
            if not text.strip():
                continue
            kind = self._classify_source_kind(path, rel_path)
            records.append(
                WorldPackSource(
                    rel_path=rel_path,
                    abs_path=str(path),
                    kind=kind,
                    size_bytes=path.stat().st_size,
                    char_count=len(text),
                    line_count=text.count("\n") + 1,
                    excerpt=_shorten(text, limit=220),
                )
            )
            source_texts.append((rel_path, kind, text))
        return records, source_texts

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="ignore")

    def _classify_source_kind(self, path: Path, rel_path: str) -> str:
        lowered = rel_path.lower()
        if path.suffix.lower() == ".json":
            return "json"
        if "mirofish_import" in lowered:
            return "mirofish_import"
        if "character" in lowered or "角色" in rel_path:
            return "character_bundle"
        return "text"

    def _build_document_text(self, source_texts: List[Tuple[str, str, str]]) -> str:
        sections: List[str] = []
        for rel_path, kind, text in source_texts:
            sections.append(f"## Source: {rel_path} ({kind})\n{text.strip()}")
        return "\n\n".join(sections).strip() + "\n"

    def _extract_entities(
        self,
        *,
        source_root: Path,
        pack_namespace: str,
        source_texts: List[Tuple[str, str, str]],
    ) -> List[EntityNode]:
        entities_by_name: Dict[str, EntityNode] = {}
        source_mentions: Dict[str, List[str]] = {}

        for rel_path, kind, text in source_texts:
            extracted: List[EntityNode] = []
            if Path(rel_path).name.lower() not in ENTITY_EXTRACTION_SKIP_FILENAMES:
                if kind == "json":
                    extracted.extend(self._extract_json_entities(pack_namespace, rel_path, text))
                extracted.extend(self._extract_markdown_entities(pack_namespace, rel_path, text))
            if not extracted and Path(rel_path).name.lower() not in ENTITY_EXTRACTION_SKIP_FILENAMES:
                extracted.extend(self._extract_file_level_entity(pack_namespace, rel_path, text))

            names_for_source = []
            for entity in extracted:
                key = self._entity_key(entity.name)
                if not key:
                    continue
                existing = entities_by_name.get(key)
                if existing is None:
                    entities_by_name[key] = entity
                else:
                    entities_by_name[key] = self._merge_entity(existing, entity)
                names_for_source.append(entities_by_name[key].name)
            source_mentions[rel_path] = names_for_source

        self._link_entities_by_source(entities_by_name, source_mentions)
        ordered = sorted(
            entities_by_name.values(),
            key=lambda item: (-len(item.summary or ""), item.name),
        )
        if ordered:
            return ordered[:120]

        fallback_name = source_root.name
        return [
            EntityNode(
                uuid=_stable_uuid(pack_namespace, fallback_name),
                name=fallback_name,
                labels=["Faction"],
                summary=fallback_name,
                attributes={"source_dir": str(source_root)},
            )
        ]

    def _extract_json_entities(self, pack_namespace: str, rel_path: str, text: str) -> List[EntityNode]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []

        entities: List[EntityNode] = []

        def append_entity(name: str, entity_type: str, summary: str, attributes: Dict[str, Any]) -> None:
            normalized_name = " ".join(str(name or "").split()).strip()
            if not normalized_name:
                return
            label = self._normalize_entity_type(entity_type or self._infer_entity_type(normalized_name, summary, rel_path))
            entities.append(
                EntityNode(
                    uuid=_stable_uuid(pack_namespace, normalized_name),
                    name=normalized_name,
                    labels=[label],
                    summary=_shorten(summary, limit=1200),
                    attributes={
                        "source_path": rel_path,
                        **{key: value for key, value in attributes.items() if value not in (None, "", [], {})},
                    },
                )
            )

        if isinstance(payload, dict) and isinstance(payload.get("characters"), list):
            for item in payload.get("characters") or []:
                if not isinstance(item, dict):
                    continue
                summary_parts = [
                    item.get("summary"),
                    item.get("background"),
                    item.get("world_role"),
                    item.get("relationship_summary"),
                ]
                attributes = {
                    "aliases": item.get("aliases"),
                    "entity_type_raw": item.get("entity_type"),
                    "faction": item.get("faction"),
                    "affiliation": item.get("affiliation"),
                    "base_of_operation": item.get("base_of_operation"),
                    "core_values": item.get("core_values"),
                    "world_functions": item.get("world_functions"),
                    "related_character_ids": item.get("related_character_ids"),
                    "key_relationships": item.get("key_relationships"),
                }
                append_entity(
                    item.get("name") or item.get("canonical_name") or item.get("id"),
                    "Character",
                    "\n".join(str(part or "").strip() for part in summary_parts if str(part or "").strip()),
                    attributes,
                )

        def walk(value: Any, *, parent_key: str = "") -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item, parent_key=parent_key)
                return
            if not isinstance(value, dict):
                return

            name = value.get("name") or value.get("title") or value.get("canonical_name")
            entity_type = (
                value.get("type")
                or value.get("entity_type")
                or value.get("类型")
                or value.get("类别")
                or parent_key
            )
            summary = (
                value.get("summary")
                or value.get("description")
                or value.get("背景")
                or value.get("摘要")
                or ""
            )
            if name and len(str(name)) <= 120:
                append_entity(
                    str(name),
                    str(entity_type or ""),
                    str(summary or ""),
                    {key: item for key, item in value.items() if key not in {"name", "title", "canonical_name", "summary", "description"}},
                )

            for key, item in value.items():
                walk(item, parent_key=key)

        walk(payload)
        return entities

    def _extract_markdown_entities(self, pack_namespace: str, rel_path: str, text: str) -> List[EntityNode]:
        entities: List[EntityNode] = []
        lines = text.splitlines()
        headings: List[Tuple[int, int, str]] = []
        for index, line in enumerate(lines):
            match = HEADING_PATTERN.match(line.strip())
            if match:
                headings.append((index, len(match.group(1)), match.group(2).strip()))

        for heading_index, (line_index, level, title) in enumerate(headings):
            if level > 3:
                continue
            next_line_index = len(lines)
            for candidate_index in range(heading_index + 1, len(headings)):
                next_pos, next_level, _ = headings[candidate_index]
                if next_level <= level:
                    next_line_index = next_pos
                    break
            section_lines = lines[line_index + 1 : next_line_index]
            section_text = "\n".join(section_lines).strip()
            if not section_text:
                continue

            section_fields = self._extract_key_value_lines(section_lines)
            raw_type = (
                section_fields.get("类型")
                or section_fields.get("实体类型")
                or section_fields.get("Type")
                or section_fields.get("Category")
            )
            entity_type = self._infer_entity_type(title, section_text, rel_path, explicit_type=raw_type)
            attributes = {
                key: value
                for key, value in section_fields.items()
                if key not in {"类型", "实体类型", "Type", "Category", "摘要", "描述", "Description"}
            }
            summary = (
                section_fields.get("摘要")
                or section_fields.get("描述")
                or section_fields.get("Description")
                or self._markdown_summary_from_section(title, section_text)
            )
            if not summary:
                continue
            entities.append(
                EntityNode(
                    uuid=_stable_uuid(pack_namespace, title),
                    name=title,
                    labels=[self._normalize_entity_type(entity_type)],
                    summary=_shorten(summary, limit=1200),
                    attributes={"source_path": rel_path, **attributes},
                )
            )

        return entities

    def _extract_file_level_entity(self, pack_namespace: str, rel_path: str, text: str) -> List[EntityNode]:
        title = Path(rel_path).stem
        summary = _shorten(text, limit=1000)
        if not summary:
            return []
        return [
            EntityNode(
                uuid=_stable_uuid(pack_namespace, title),
                name=title,
                labels=[self._normalize_entity_type(self._infer_entity_type(title, text, rel_path))],
                summary=summary,
                attributes={"source_path": rel_path},
            )
        ]

    def _extract_key_value_lines(self, lines: Iterable[str]) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for raw_line in lines:
            match = SECTION_KEY_PATTERN.match(raw_line)
            if not match:
                continue
            key = match.group(1).strip()
            value = match.group(2).strip()
            if key and value and key not in fields:
                fields[key] = value
        return fields

    def _markdown_summary_from_section(self, title: str, section_text: str) -> str:
        prose_lines: List[str] = []
        for line in section_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if SECTION_KEY_PATTERN.match(stripped):
                continue
            prose_lines.append(stripped.lstrip("- ").strip())
            if len(" ".join(prose_lines)) >= 240:
                break
        if prose_lines:
            return " ".join(prose_lines)
        return title

    def _entity_key(self, name: str) -> str:
        normalized = " ".join(str(name or "").split()).strip().lower()
        normalized = re.sub(r"[()（）\[\]【】]", "", normalized)
        return normalized

    def _merge_entity(self, left: EntityNode, right: EntityNode) -> EntityNode:
        labels = list(dict.fromkeys([*(left.labels or []), *(right.labels or [])]))
        summary = left.summary if len(left.summary or "") >= len(right.summary or "") else right.summary
        attributes = dict(left.attributes or {})
        for key, value in (right.attributes or {}).items():
            if key not in attributes or attributes[key] in ("", [], {}, None):
                attributes[key] = value
        related_edges = list(left.related_edges or [])
        related_nodes = list(left.related_nodes or [])
        for item in right.related_edges or []:
            if item not in related_edges:
                related_edges.append(item)
        for item in right.related_nodes or []:
            if item not in related_nodes:
                related_nodes.append(item)
        return EntityNode(
            uuid=left.uuid,
            name=left.name if len(left.name) >= len(right.name) else right.name,
            labels=labels,
            summary=summary,
            attributes=attributes,
            related_edges=related_edges[:16],
            related_nodes=related_nodes[:16],
        )

    def _normalize_entity_type(self, raw_type: str) -> str:
        normalized = str(raw_type or "").strip()
        if not normalized:
            return "Character"
        normalized = normalized.split("|", 1)[0].split("/", 1)[0].strip()
        if normalized.lower() in {"human", "person"}:
            return "Character"
        if normalized.lower() in {"organization", "institution"}:
            return normalized.title()
        return normalized[0].upper() + normalized[1:] if normalized else "Character"

    def _infer_entity_type(
        self,
        title: str,
        section_text: str,
        rel_path: str,
        *,
        explicit_type: Optional[str] = None,
    ) -> str:
        explicit = str(explicit_type or "").strip()
        if explicit:
            normalized = explicit.split("|", 1)[0].split("/", 1)[0].strip()
            if normalized:
                return normalized

        lowered = f"{title}\n{section_text}\n{rel_path}".lower()
        if any(token in lowered for token in ("faction", "势力", "组织", "government", "海贼团", "革命军", "海军")):
            return "Faction"
        if any(token in lowered for token in ("character", "角色", "人物", "person")):
            return "Character"
        if any(token in lowered for token in ("place", "地点", "region", "island", "country", "route", "海域", "王国")):
            return "Place"
        if any(token in lowered for token in ("rule", "system", "resource", "secret", "conflict", "机制", "资源", "秘密", "矛盾")):
            return "Rule"
        return "Character"

    def _link_entities_by_source(
        self,
        entities_by_name: Dict[str, EntityNode],
        source_mentions: Dict[str, List[str]],
    ) -> None:
        name_to_key = {entity.name: key for key, entity in entities_by_name.items()}
        for rel_path, names in source_mentions.items():
            unique_names = []
            seen = set()
            for name in names:
                key = name_to_key.get(name) or self._entity_key(name)
                if key and key not in seen and key in entities_by_name:
                    unique_names.append(key)
                    seen.add(key)
            for key in unique_names:
                entity = entities_by_name[key]
                related_nodes = list(entity.related_nodes or [])
                related_edges = list(entity.related_edges or [])
                for other_key in unique_names:
                    if other_key == key:
                        continue
                    other = entities_by_name[other_key]
                    node_payload = {
                        "uuid": other.uuid,
                        "name": other.name,
                        "labels": other.labels,
                    }
                    if node_payload not in related_nodes:
                        related_nodes.append(node_payload)
                edge_payload = {
                    "name": "co-mentioned",
                    "fact": f"Appears together in {rel_path}",
                    "attributes": {"source_path": rel_path},
                }
                if edge_payload not in related_edges:
                    related_edges.append(edge_payload)
                entity.related_nodes = related_nodes[:12]
                entity.related_edges = related_edges[:12]

    def _select_actor_entities(self, all_entities: List[EntityNode]) -> List[EntityNode]:
        actors = [
            entity
            for entity in all_entities
            if self._entity_key(entity.get_entity_type() or "") in ACTOR_ENTITY_TYPES
        ]
        if actors:
            return actors[:64]
        return []

    def _default_requirement(self, pack_title: str) -> str:
        return (
            f"基于《{pack_title}》资料推进一个并发 world simulation。重点不是复述设定，而是让核心角色、"
            "主要势力与关键地点在同一时间窗口内持续相互作用，形成会连锁放大的世界演进。"
        )

    def _bootstrap_project(
        self,
        *,
        project_id: str,
        project_name: str,
        graph_id: str,
        simulation_requirement: str,
        document_text: str,
        source_dir: str,
    ):
        project = ProjectManager.get_project(project_id) if project_id else None
        if project is None:
            project = ProjectManager.create_project(
                name=project_name,
                simulation_mode=SimulationMode.WORLD.value,
            )
        project.graph_id = graph_id or project.graph_id or f"world_pack_{uuid.uuid4().hex[:12]}"
        project.simulation_requirement = simulation_requirement.strip() or project.simulation_requirement or self._default_requirement(project_name)
        project.analysis_summary = f"Bootstrapped from local world pack: {source_dir}"
        project.status = ProjectStatus.GRAPH_COMPLETED
        ProjectManager.save_project(project)
        ProjectManager.save_extracted_text(project.project_id, document_text)
        return project

    def _bootstrap_simulation_state(
        self,
        *,
        simulation_id: str,
        project_id: str,
        graph_id: str,
    ) -> SimulationState:
        state = self._simulation_manager.get_simulation(simulation_id)
        if state is None:
            state = SimulationState(
                simulation_id=simulation_id,
                project_id=project_id,
                graph_id=graph_id,
                simulation_mode=SimulationMode.WORLD.value,
                enable_twitter=False,
                enable_reddit=False,
                status=SimulationStatus.READY,
            )
        else:
            state.project_id = project_id
            state.graph_id = graph_id
            state.simulation_mode = SimulationMode.WORLD.value
            state.enable_twitter = False
            state.enable_reddit = False
            state.status = SimulationStatus.READY
            state.error = None
        self._simulation_manager._save_simulation_state(state)
        return state

    def _hydrate_runtime_metadata(
        self,
        *,
        state: SimulationState,
        world_preset_id: str,
        entity_count: int,
        actor_entity_count: int,
        profile_count: int,
        entity_types: List[str],
        runtime_config: Dict[str, Any],
        pack_id: str,
        pack_title: str,
    ) -> None:
        world_preset = WorldPresetRegistry.get_preset(world_preset_id)
        actor_llm_config = Config.get_llm_config(selector=world_preset.actor_selector or "WORLD_AGENT")
        resolver_llm_config = Config.get_llm_config(selector=world_preset.resolver_selector or "WORLD_RESOLVER")
        state.entities_count = entity_count
        state.profiles_count = profile_count
        state.entity_types = entity_types
        state.config_generated = True
        state.config_reasoning = "Compiled from local source materials via world pack compiler."
        state.runtime_metadata = {
            "profile_format": "world",
            "profiles_file": "world_profiles.json",
            "world_preset_id": world_preset.preset_id,
            "world_preset_label": world_preset.label,
            "world_preset_strategy_class": world_preset.strategy_class,
            "world_preset_actor_selector": world_preset.actor_selector,
            "world_preset_resolver_selector": world_preset.resolver_selector,
            "world_preset_tags": world_preset.tags,
            "actor_count": profile_count,
            "entity_count": entity_count,
            "actor_entity_count": actor_entity_count,
            "runtime_config": runtime_config,
            "world_pack_id": pack_id,
            "world_pack_title": pack_title,
            "world_agent_model": actor_llm_config.get("model_name"),
            "world_agent_provider": actor_llm_config.get("provider_id"),
            "world_agent_profile": actor_llm_config.get("profile_id"),
            "world_resolver_model": resolver_llm_config.get("model_name"),
            "world_resolver_provider": resolver_llm_config.get("provider_id"),
            "world_resolver_profile": resolver_llm_config.get("profile_id"),
        }
        self._simulation_manager._save_simulation_state(state)

    def _build_digest_markdown(
        self,
        *,
        pack_title: str,
        source_dir: str,
        source_records: List[WorldPackSource],
        all_entities: List[EntityNode],
        actor_entities: List[EntityNode],
    ) -> str:
        lines = [
            f"# World Pack Digest: {pack_title}",
            "",
            f"- Source Dir: `{source_dir}`",
            f"- Source Files: `{len(source_records)}`",
            f"- Compiled Entities: `{len(all_entities)}`",
            f"- Actor Entities: `{len(actor_entities)}`",
            "",
            "## Sources",
            "",
        ]
        for record in source_records:
            lines.append(
                f"- `{record.rel_path}` ({record.kind}, {record.char_count} chars): {record.excerpt}"
            )
        lines.extend(["", "## Compiled Actors", ""])
        for entity in actor_entities[:24]:
            lines.append(
                f"- `{entity.name}` [{entity.get_entity_type() or 'Unknown'}]: {_shorten(entity.summary, 160)}"
            )
        lines.extend(["", "## Additional World Entities", ""])
        for entity in all_entities[:24]:
            if entity in actor_entities:
                continue
            lines.append(
                f"- `{entity.name}` [{entity.get_entity_type() or 'Unknown'}]: {_shorten(entity.summary, 160)}"
            )
        return "\n".join(lines).rstrip() + "\n"

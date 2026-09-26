"""Per-job OpenCode runtime config injection.

The worker materializes a **known** OpenCode configuration at the job's location
directory (OpenCode's cwd, injected as *project* config) so a task runs with the
team's agents, skills and instructions regardless of the machine, without the
plan author rewriting Markdown bodies.

Sources, lowest to highest precedence:

0. a built-in default granting every agent read/write on the platform temp
   directory (``temp_permissions``), so a scratch file under ``$TMPDIR`` /
   ``%TEMP%`` never trips OpenCode's auto-rejected ``external_directory`` ask;
1. the worker's baseline OpenCode bundle (``--opencode-dir``);
2. repo/config provenance reported by the git/path providers;
3. plan ``opencode`` sources (``from``, ``agents_paths``, ``skills_paths``,
   ``agents_md`` or the verbose ``sources`` list);
4. plan inline ``config`` / ``agents`` / ``skills``.

Materialization rules (see the design doc, section 9.3):

- **config** fragments are deep-merged into ``<location>/opencode.json``;
- **agents** are symlinked (junction/copy fallback) file by file into
  ``<location>/.opencode/agents/`` (OpenCode has no agent-directory config);
- **skills** are referenced by absolute path in the config ``skills`` array
  (OpenCode supports it natively);
- **AGENTS.md** files are concatenated into ``<location>/AGENTS.md`` with a
  ``<!-- source: ... -->`` marker, except a repo ``AGENTS.md`` that already
  lives below the location (OpenCode discovers it by itself).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .environment import EnvironmentError, resolve_worker_path, temp_permissions

logger = logging.getLogger("hiveling.worker.opencode")

_AGENT_DIRS = ("agents", "agent", "mode", "modes")
_SKILL_DIRS = ("skills", "skill")
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    """Best-effort JSONC -> JSON (strip comments and trailing commas)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return _TRAILING_COMMA.sub(r"\1", "".join(out))


def _read_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        value = json.loads(_strip_jsonc(raw))
    except json.JSONDecodeError as exc:
        raise EnvironmentError(f"invalid OpenCode config {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


# Lists under these keys are concatenated (deduplicated, order preserved) when
# merging layers, instead of being replaced. This keeps permission rules from
# several layers (worker baseline, repo, plan, providers) and every skill/plugin
# declared along the way.
_ADDITIVE_KEYS = {"permissions", "skills", "plugins"}


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto ``base`` (later layers win)."""
    merged = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        elif (
            isinstance(value, list)
            and isinstance(current, list)
            and key in _ADDITIVE_KEYS
        ):
            combined = list(current)
            for item in value:
                if item not in combined:
                    combined.append(item)
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def _merge_into(target: dict, over: dict) -> None:
    """In-place :func:`_deep_merge` (keeps the caller's dict identity)."""
    merged = _deep_merge(target, over)
    target.clear()
    target.update(merged)


@dataclass
class Source:
    kind: str
    path: Path | None
    mode: str = "auto"
    priority: int = 0
    optional: bool = False


@dataclass
class InjectionReport:
    config: dict = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    agents_md: list[str] = field(default_factory=list)


def _collect(root: Path, *, bundle: bool) -> tuple[list, list, list, list]:
    """Return (config_paths, agent_dirs, skill_dirs, agents_md) under ``root``."""
    configs: list[Path] = []
    agents: list[Path] = []
    skills: list[Path] = []
    md: list[Path] = []

    if bundle:
        for name in _CONFIG_NAMES:
            if (root / name).is_file():
                configs.append(root / name)
        if (root / "agents").is_dir():
            agents.append(root / "agents")
        if (root / "skills").is_dir():
            skills.append(root / "skills")
        if (root / "AGENTS.md").is_file():
            md.append(root / "AGENTS.md")
        return configs, agents, skills, md

    for name in _CONFIG_NAMES:
        if (root / name).is_file():
            configs.append(root / name)
    dot = root / ".opencode"
    if dot.is_dir():
        for name in _CONFIG_NAMES:
            if (dot / name).is_file():
                configs.append(dot / name)
        for name in _AGENT_DIRS:
            if (dot / name).is_dir():
                agents.append(dot / name)
        for name in _SKILL_DIRS:
            if (dot / name).is_dir():
                skills.append(dot / name)
    for extra in (root / ".claude" / "skills", root / ".agents" / "skills"):
        if extra.is_dir():
            skills.append(extra)
    if (root / "AGENTS.md").is_file():
        md.append(root / "AGENTS.md")
    return configs, agents, skills, md


def _link_or_copy(src: Path, dst: Path) -> str:
    """Symlink ``src`` to ``dst`` (junction on Windows dirs), else copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        except OSError:
            pass
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
        return "symlink"
    except OSError:
        pass
    if os.name == "nt" and src.is_dir():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return "copy"


def _parse_plan_sources(plan_opencode: dict) -> list[Source]:
    sources: list[Source] = []
    for path in plan_opencode.get("from") or []:
        sources.append(Source("bundle", Path(str(path))))
    for path in plan_opencode.get("agents_paths") or []:
        sources.append(Source("agents", Path(str(path))))
    for path in plan_opencode.get("skills_paths") or []:
        sources.append(Source("skills", Path(str(path))))
    for path in plan_opencode.get("agents_md") or []:
        sources.append(Source("agents_md", Path(str(path))))
    for raw in plan_opencode.get("sources") or []:
        if not isinstance(raw, dict):
            raise EnvironmentError(f"opencode: invalid source: {raw!r}")
        sources.append(
            Source(
                kind=str(raw.get("kind") or "bundle"),
                path=Path(str(raw.get("path"))) if raw.get("path") else None,
                mode=str(raw.get("mode") or "auto"),
                priority=int(raw.get("priority") or 0),
                optional=bool(raw.get("optional", False)),
            )
        )
    for source in sources:
        if source.path is not None and not source.path.is_absolute():
            # Relative to the plan is not meaningful on the worker: treat as
            # worker-relative and let resolve_worker_path apply path_roots.
            pass
    return sources


def _resolve(source_path: Path, ctx, label: str) -> Path | None:
    try:
        return resolve_worker_path(str(source_path), ctx, label=label)
    except EnvironmentError:
        raise


def inject(
    location: Path,
    *,
    plan_opencode: dict | None,
    baseline_dir: Path | None,
    provenance: list[tuple[Path, bool]] | None,
    fragments: list[dict] | None,
    ctx,
) -> InjectionReport:
    """Materialize the effective OpenCode config at ``location``."""
    plan_opencode = plan_opencode or {}
    fragments = fragments or []
    provenance = provenance or []
    report = InjectionReport()

    location.mkdir(parents=True, exist_ok=True)
    # Lowest-priority layer: every agent may read/write the platform temp dir.
    # OpenCode auto-rejects `ask` in a non-interactive run, so without this a
    # scratch file under $TMPDIR/%TEMP% aborts the session. Later layers (repo,
    # plan, agent rules) still win because `permissions` is additive.
    config: dict = temp_permissions()
    agent_dirs: list[tuple[int, Path]] = []
    skill_dirs: list[tuple[int, Path]] = []
    agents_md: list[tuple[int, Path]] = []

    def add_root(root: Path, *, bundle: bool, priority: int) -> None:
        configs, agents, skills, md = _collect(root, bundle=bundle)
        for path in configs:
            _merge_into(config, _read_config(path))
        for path in agents:
            agent_dirs.append((priority, path))
        for path in skills:
            skill_dirs.append((priority, path))
        for path in md:
            agents_md.append((priority, path))

    # 1. worker baseline bundle
    if baseline_dir and baseline_dir.is_dir():
        add_root(baseline_dir, bundle=True, priority=-100)

    # 2. repo/config provenance from providers
    for root, under_location in provenance:
        configs, agents, skills, md = _collect(root, bundle=False)
        for path in configs:
            _merge_into(config, _read_config(path))
        for path in agents:
            agent_dirs.append((0, path))
        for path in skills:
            skill_dirs.append((0, path))
        for path in md:
            # A repo AGENTS.md below the location is discovered by OpenCode.
            if not under_location:
                agents_md.append((0, path))

    # 3. plan path sources
    for index, source in enumerate(_parse_plan_sources(plan_opencode)):
        priority = source.priority if source.priority else index
        if source.path is None:
            continue
        path = resolve_worker_path(str(source.path), ctx, label="opencode")
        if not path.exists():
            if source.optional:
                continue
            raise EnvironmentError(f"opencode: source path not found: {path}")
        if source.kind == "bundle":
            add_root(path, bundle=True, priority=priority)
            continue
        configs, agents, skills, md = _collect(path, bundle=source.kind == "bundle")
        if source.kind == "config":
            for config_path in configs:
                _merge_into(config, _read_config(config_path))
            if path.is_file():
                _merge_into(config, _read_config(path))
        elif source.kind == "agents":
            agent_dirs.append((priority, path))
        elif source.kind == "skills":
            skill_dirs.append((priority, path))
        elif source.kind == "agents_md":
            agents_md.append((priority, path))

    # 4. plan inline config
    inline_config = plan_opencode.get("config")
    if isinstance(inline_config, dict):
        config = _deep_merge(config, inline_config)

    # config fragments contributed by providers (e.g. external permissions)
    for fragment in fragments:
        if isinstance(fragment, dict):
            config = _deep_merge(config, fragment)

    # --- skills: reference absolute directories in the config array --------
    referenced_skills: list[str] = []
    for _, path in sorted(skill_dirs, key=lambda item: item[0]):
        referenced = str(path.resolve())
        if referenced not in referenced_skills:
            referenced_skills.append(referenced)
    if referenced_skills:
        existing = config.get("skills")
        merged = list(existing) if isinstance(existing, list) else []
        for entry in referenced_skills:
            if entry not in merged:
                merged.append(entry)
        config["skills"] = merged
    report.skills = referenced_skills

    # --- agents: link each markdown file into .opencode/agents ------------
    agents_target = location / ".opencode" / "agents"
    inline_agents = plan_opencode.get("agents")
    for _, path in sorted(agent_dirs, key=lambda item: item[0]):
        for md_file in sorted(path.rglob("*.md")):
            rel = md_file.relative_to(path)
            _link_or_copy(md_file, agents_target / rel)
            report.agents.append(str(agents_target / rel))
    if isinstance(inline_agents, dict):
        for name, body in inline_agents.items():
            target = agents_target / f"{name}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(body).rstrip() + "\n", encoding="utf-8")
            report.agents.append(str(target))

    # --- inline skills: write .opencode/skills/<name>/SKILL.md ------------
    inline_skills = plan_opencode.get("skills")
    if isinstance(inline_skills, list):
        for entry in inline_skills:
            if not isinstance(entry, dict) or not entry.get("name"):
                raise EnvironmentError("opencode: inline skill needs a 'name'")
            target_dir = location / ".opencode" / "skills" / str(entry["name"])
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "SKILL.md").write_text(
                str(entry.get("content", "")).rstrip() + "\n", encoding="utf-8"
            )

    # --- AGENTS.md: concatenate declared sources --------------------------
    if agents_md:
        parts: list[str] = []
        for _, path in sorted(agents_md, key=lambda item: item[0]):
            try:
                text = path.read_text(encoding="utf-8-sig").rstrip()
            except OSError:
                continue
            parts.append(f"<!-- source: {path} -->\n{text}")
            report.agents_md.append(str(path))
        (location / "AGENTS.md").write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    # --- write the merged config ------------------------------------------
    if config:
        config.setdefault("$schema", "https://opencode.ai/config.json")
        (location / "opencode.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    report.config = config
    return report

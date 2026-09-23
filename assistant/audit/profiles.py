from __future__ import annotations

from .models import AuditProfile, Depth, TargetKind

PROFILES: dict[str, AuditProfile] = {}


def register_profile(profile: AuditProfile) -> AuditProfile:
    PROFILES[profile.name] = profile
    return profile


def get_profile(name: str = "general") -> AuditProfile:
    return PROFILES.get(name, PROFILES["general"])


register_profile(AuditProfile(
    name="general", target_kinds=list(TargetKind), default_depth=Depth.STANDARD,
    default_scope=[
        "structure", "architecture", "quality", "configuration",
        "security", "testing", "documentation", "operations",
    ],
    description="Domain-neutral read-only audit.",
))

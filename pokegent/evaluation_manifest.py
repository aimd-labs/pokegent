"""Default-off projection of caller attempt manifests (#138/R09); not a loader or validator."""

from __future__ import annotations

from typing import Any

from pokegent.evaluation_campaign import (
    MAX_ATTEMPTS,
    CampaignAttempt,
    CampaignAttemptRejected,
    summarize_attempts,
)

SCHEMA_VERSION = "pokegent-evaluation/1"
PROJECTED = ("campaign_id", "attempt_id", "status", "operator_interventions")


def project_attempt(
    manifest: Any, *, cohort_id: Any, enabled: bool = False
) -> CampaignAttempt | None:
    """Project four caller fields into CampaignAttempt only when ``enabled is True``."""
    if type(enabled) is not bool:
        raise CampaignAttemptRejected("enabled must be a bool")
    if not enabled:
        return None
    if type(manifest) is not dict:
        raise CampaignAttemptRejected("manifest must be a plain dict")
    schema = manifest.get("schema_version")
    if type(schema) is not str or schema != SCHEMA_VERSION:
        raise CampaignAttemptRejected(
            f"schema_version must be exactly {SCHEMA_VERSION!r}"
        )
    for name in PROJECTED:
        if name not in manifest:
            raise CampaignAttemptRejected(f"{name} is required")
    return CampaignAttempt(
        campaign_id=manifest["campaign_id"],
        attempt_id=manifest["attempt_id"],
        cohort_id=cohort_id,
        status=manifest["status"],
        operator_interventions=manifest["operator_interventions"],
    )


def summarize_manifests(
    manifests: Any, *, cohort_id: Any, enabled: bool = False
) -> dict[str, Any]:
    """Summarize a plain manifest batch via the existing helpers when enabled."""
    if type(enabled) is not bool:
        raise CampaignAttemptRejected("enabled must be a bool")
    if not enabled:
        return summarize_attempts((), enabled=False)
    if type(manifests) not in (list, tuple):
        raise CampaignAttemptRejected("manifests must be a plain list or tuple")
    if len(manifests) > MAX_ATTEMPTS:
        raise CampaignAttemptRejected(f"manifests exceeds {MAX_ATTEMPTS}")
    attempts = [
        project_attempt(manifest, cohort_id=cohort_id, enabled=True)
        for manifest in manifests
    ]
    return summarize_attempts(attempts, enabled=True)

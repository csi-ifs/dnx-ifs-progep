"""AWS provider configuration with global APN tagging and a single stack.

Infrastructure-as-Code module (Task 13.1) for the ``Provisionador_IaC``. It
provides:

* **Global tagging configuration (Req 8.1, 8.4)** — a single, canonical tagging
  definition based on :data:`~src.domain.config.APN_TAG` that is applied to
  100% of taggable AWS resources through the Pulumi AWS provider's
  ``default_tags``. Because the tag set is defined once and injected at the
  provider level, every resource created with this provider inherits the
  ``Tag_APN`` without per-resource configuration.
* **Single stack/environment per execution (Req 9.1, 9.2)** —
  :func:`enforce_single_environment` guarantees that a single execution targets
  exactly one designated environment, refusing to proceed when zero or more than
  one environment is requested.
* **Tagging-exception registry (Req 8.6)** — :class:`TaggingExceptionRegistry`
  records resources that do **not** support tags together with the reason the
  ``Tag_APN`` could not be applied, so the exception is auditable.

Design constraints honoured here:

* The ``pulumi`` / ``pulumi_aws`` imports are **guarded** so this module imports
  cleanly even when those packages are not installed. The tagging and
  exception-registration logic is expressed as **pure helpers** with no
  dependency on Pulumi, which keeps them unit-testable in any environment.
* Functions that actually construct Pulumi resources raise a clear error when
  Pulumi is unavailable, rather than failing at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from src.domain.config import APN_TAG

# --- Guarded Pulumi imports -----------------------------------------------------
# The module must import even when pulumi / pulumi_aws are absent; provider
# construction is deferred and guarded by :func:`_require_pulumi`.
try:  # pragma: no cover - depends on the runtime environment
    import pulumi
    import pulumi_aws as aws

    _PULUMI_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the runtime environment
    pulumi = None  # type: ignore[assignment]
    aws = None  # type: ignore[assignment]
    _PULUMI_AVAILABLE = False


#: Default logical name for the single global AWS provider.
DEFAULT_PROVIDER_NAME: str = "aws"


def pulumi_available() -> bool:
    """Return ``True`` when ``pulumi`` and ``pulumi_aws`` are importable."""
    return _PULUMI_AVAILABLE


def _require_pulumi() -> None:
    """Raise a clear error when a Pulumi operation is attempted without Pulumi."""
    if not _PULUMI_AVAILABLE:
        raise RuntimeError(
            "pulumi/pulumi_aws não estão instalados; instale-os para provisionar "
            "recursos (pip install pulumi pulumi-aws). As funções de tagueamento "
            "puras podem ser usadas sem o Pulumi."
        )


# --- Pure tagging helpers (no Pulumi required) ----------------------------------


def apn_default_tags() -> dict[str, str]:
    """Return the single global tag set applied to all taggable resources.

    This is the one canonical tagging definition required by Req 8.4: the
    :data:`~src.domain.config.APN_TAG` (``aws-apn-id: pc:...``). A defensive copy
    is returned so callers cannot mutate the shared constant.
    """
    return dict(APN_TAG)


def build_default_tags(extra_tags: dict[str, str] | None = None) -> dict[str, str]:
    """Build the effective default-tags map for the provider (Req 8.1, 8.4).

    The mandatory ``Tag_APN`` is always present and takes precedence: any
    ``extra_tags`` are merged first, then the APN tag is applied on top so it can
    never be overridden by a caller-supplied value.

    Args:
        extra_tags: Optional additional tags to apply globally alongside the
            mandatory APN tag.

    Returns:
        A new dict containing the merged tags with the APN tag guaranteed present.
    """
    merged: dict[str, str] = {}
    if extra_tags:
        merged.update(extra_tags)
    merged.update(APN_TAG)  # APN tag wins; never overridable (Req 8.1)
    return merged


def build_provider_args(
    *,
    region: str | None = None,
    profile: str | None = None,
    extra_tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a plain kwargs dict for the AWS provider (pure, Pulumi-free).

    Separating argument assembly from resource construction lets the tagging
    configuration be asserted in unit tests without importing Pulumi. The
    returned mapping always carries a ``default_tags.tags`` entry containing the
    single global APN tag set (Req 8.4).

    Args:
        region: Optional AWS region for the provider.
        profile: Optional shared-config profile name.
        extra_tags: Optional additional global tags merged with the APN tag.

    Returns:
        A dict of keyword arguments suitable for constructing the AWS provider,
        with a nested ``{"default_tags": {"tags": {...}}}`` structure.
    """
    args: dict[str, Any] = {
        "default_tags": {"tags": build_default_tags(extra_tags)},
    }
    if region is not None:
        args["region"] = region
    if profile is not None:
        args["profile"] = profile
    return args


# --- Single stack / environment enforcement (Req 9.1, 9.2) ----------------------


class MultipleEnvironmentsError(ValueError):
    """Raised when an execution would target more than one environment (Req 9.2)."""


class MissingEnvironmentError(ValueError):
    """Raised when no environment is designated for the execution (Req 9.2)."""


def enforce_single_environment(environments: Iterable[str]) -> str:
    """Guarantee exactly one designated environment per execution (Req 9.2).

    The ``Provisionador_IaC`` provisions the solution in a single environment per
    execution and in no other. This helper collapses the requested environment
    names to the single distinct value, raising when that invariant is violated.

    Args:
        environments: The environment/stack names requested for this execution.

    Returns:
        The single, distinct environment name.

    Raises:
        MissingEnvironmentError: If no (non-empty) environment is provided.
        MultipleEnvironmentsError: If more than one distinct environment is
            provided.
    """
    distinct: list[str] = []
    for env in environments:
        name = (env or "").strip()
        if not name:
            continue
        if name not in distinct:
            distinct.append(name)

    if not distinct:
        raise MissingEnvironmentError(
            "nenhum ambiente designado para a execução (Req 9.2)"
        )
    if len(distinct) > 1:
        raise MultipleEnvironmentsError(
            "execução não pode provisionar em múltiplos ambientes: "
            f"{', '.join(distinct)} (Req 9.2)"
        )
    return distinct[0]


# --- Tagging-exception registry (Req 8.6) ---------------------------------------


@dataclass(frozen=True)
class TaggingException:
    """A recorded exception for a resource that does not support tags (Req 8.6).

    Attributes:
        resource_id: Identification of the AWS resource that cannot be tagged.
        reason: The reason the ``Tag_APN`` could not be applied.
    """

    resource_id: str
    reason: str


@dataclass
class TaggingExceptionRegistry:
    """Registry of resources that do not support tags (Req 8.6).

    When a provisioned AWS resource does not support tags, the
    ``Provisionador_IaC`` records a tagging exception containing the resource's
    identification and the reason the ``Tag_APN`` could not be applied. This
    registry is a pure, in-memory collection so it can be populated and inspected
    without any Pulumi dependency.
    """

    _exceptions: list[TaggingException] = field(default_factory=list)

    def register(self, resource_id: str, reason: str) -> TaggingException:
        """Record a tagging exception for a non-taggable resource (Req 8.6).

        Args:
            resource_id: Identification of the resource (non-empty).
            reason: Why the ``Tag_APN`` could not be applied (non-empty).

        Returns:
            The recorded :class:`TaggingException`.

        Raises:
            ValueError: If ``resource_id`` or ``reason`` is empty/blank.
        """
        rid = (resource_id or "").strip()
        why = (reason or "").strip()
        if not rid:
            raise ValueError("resource_id da exceção de tagueamento não pode ser vazio")
        if not why:
            raise ValueError("reason da exceção de tagueamento não pode ser vazio")
        exception = TaggingException(resource_id=rid, reason=why)
        self._exceptions.append(exception)
        return exception

    def contains(self, resource_id: str) -> bool:
        """Return ``True`` if an exception is registered for ``resource_id``."""
        rid = (resource_id or "").strip()
        return any(exc.resource_id == rid for exc in self._exceptions)

    @property
    def exceptions(self) -> tuple[TaggingException, ...]:
        """The recorded tagging exceptions, as an immutable tuple."""
        return tuple(self._exceptions)

    def as_dicts(self) -> list[dict[str, str]]:
        """Render the registry as a list of ``{id, reason}`` dicts for logging."""
        return [
            {"resource_id": exc.resource_id, "reason": exc.reason}
            for exc in self._exceptions
        ]

    def __len__(self) -> int:
        return len(self._exceptions)


#: A process-wide registry instance for convenience during provisioning.
tagging_exceptions = TaggingExceptionRegistry()


# --- Pulumi provider construction (guarded) -------------------------------------


def configure_provider(
    name: str = DEFAULT_PROVIDER_NAME,
    *,
    region: str | None = None,
    profile: str | None = None,
    extra_tags: dict[str, str] | None = None,
) -> Any:
    """Create the single global AWS provider with default APN tagging.

    Builds a :class:`pulumi_aws.Provider` whose ``default_tags`` carry the single
    global APN tag set, so every resource created with this provider is tagged
    with the ``Tag_APN`` (Req 8.1, 8.4). The provider arguments are assembled by
    the pure :func:`build_provider_args`, which is independently testable.

    Args:
        name: Logical name for the provider resource.
        region: Optional AWS region.
        profile: Optional shared-config profile.
        extra_tags: Optional additional global tags merged with the APN tag.

    Returns:
        A configured ``pulumi_aws.Provider`` instance.

    Raises:
        RuntimeError: If Pulumi is not available in the environment.
    """
    _require_pulumi()
    args = build_provider_args(region=region, profile=profile, extra_tags=extra_tags)
    default_tags = aws.ProviderDefaultTagsArgs(tags=args["default_tags"]["tags"])
    provider_kwargs: dict[str, Any] = {"default_tags": default_tags}
    if "region" in args:
        provider_kwargs["region"] = args["region"]
    if "profile" in args:
        provider_kwargs["profile"] = args["profile"]
    return aws.Provider(name, **provider_kwargs)

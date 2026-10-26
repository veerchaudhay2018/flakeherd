"""Optional JSON configuration file support (``.flakeherd.json``).

Precedence (low to high): built-in defaults < config file < CLI flags.
The file is deliberately optional; every setting has a sane default so a
first-time user can run the CLI with no configuration at all.

Quarantine policy
-----------------
The score-threshold quarantine policy is **opt-in**. With
``quarantine_threshold_enabled: false`` (the default) selection behaves
exactly like flakeherd 0.1.x: every *confirmed-flaky* / *flaky* test
qualifies (subject to ``quarantine_min_score``). When the policy is
enabled, three additional guards apply:

* ``quarantine_score_threshold`` (default 40) - the minimum flakiness
  score; explicit values (config or ``--min-score``) can only *raise* the
  effective threshold, never lower it below the policy floor.
* ``quarantine_min_samples`` (default 5) - tests with less recorded
  history never qualify for automated quarantine.
* suggestion mode - ``flakeherd record`` merely *suggests* candidates;
  writing the manifest requires the explicit ``flakeherd quarantine``
  command or ``record --auto-quarantine``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import FlakeHerdError

CONFIG_FILENAME = ".flakeherd.json"

DEFAULT_QUARANTINE_SCORE_THRESHOLD = 40
DEFAULT_QUARANTINE_MIN_SAMPLES = 5

_INT_FIELDS = (
    "window",
    "max_runs",
    "min_regression_streak",
    "max_annotations",
    "quarantine_min_score",
    "quarantine_score_threshold",
    "quarantine_min_samples",
    "quarantine_expiry_days",
)
_BOOL_FIELDS = ("include_low_confidence", "quarantine_threshold_enabled")


@dataclass
class Config:
    """Runtime settings shared by the detector, reporters and quarantine."""

    window: int = 30
    """How many recent outcomes per test to consider (older samples are
    ignored by the detector)."""

    max_runs: int = 500
    """Upper bound on runs kept in the SQLite store (pruning)."""

    min_regression_streak: int = 2
    """Consecutive trailing failures (after passes) required before a test is
    classified as a regression rather than a possible first flake."""

    max_annotations: int = 10
    """Cap on GitHub annotation commands printed per severity level."""

    ignore: list[str] = field(default_factory=list)
    """fnmatch patterns matched (case-insensitively) against test keys,
    classnames and test names; matching tests are excluded from analysis."""

    quarantine_min_score: int = 0
    """Minimum flakiness score (0-100) for a test to enter the manifest."""

    include_low_confidence: bool = False
    """Also quarantine ``flaky-low`` and ``suspected-flaky`` classifications."""

    quarantine_threshold_enabled: bool = False
    """Opt-in switch for the score-threshold quarantine policy. When true,
    the policy floor (``quarantine_score_threshold``) and the minimum-sample
    guard (``quarantine_min_samples``) are enforced, and ``record`` reports
    quarantine suggestions instead of silently writing a manifest."""

    quarantine_score_threshold: int = DEFAULT_QUARANTINE_SCORE_THRESHOLD
    """Policy floor for the quarantine score (applies only when
    ``quarantine_threshold_enabled`` is true)."""

    quarantine_min_samples: int = DEFAULT_QUARANTINE_MIN_SAMPLES
    """Minimum recorded samples before a test may be quarantined while the
    policy is enabled (sufficient-evidence guard)."""

    quarantine_expiry_days: int = 0
    """When > 0, new manifest entries carry an ``expires_at`` date this many
    days after generation; expired entries stop being skipped by the pytest
    plugin (quarantine lapses and the test runs again). 0 = never expires."""

    def validated(self) -> Config:
        if self.window < 2:
            raise FlakeHerdError("config: 'window' must be >= 2")
        if self.max_runs < 1:
            raise FlakeHerdError("config: 'max_runs' must be >= 1")
        if self.min_regression_streak < 1:
            raise FlakeHerdError("config: 'min_regression_streak' must be >= 1")
        if self.max_annotations < 0:
            raise FlakeHerdError("config: 'max_annotations' must be >= 0")
        if self.quarantine_min_score < 0 or self.quarantine_min_score > 100:
            raise FlakeHerdError("config: 'quarantine_min_score' must be 0..100")
        if self.quarantine_score_threshold < 0 or self.quarantine_score_threshold > 100:
            raise FlakeHerdError(
                "config: 'quarantine_score_threshold' must be 0..100"
            )
        if self.quarantine_min_samples < 1:
            raise FlakeHerdError("config: 'quarantine_min_samples' must be >= 1")
        if self.quarantine_expiry_days < 0:
            raise FlakeHerdError("config: 'quarantine_expiry_days' must be >= 0")
        return self

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        """Load config from *path*; missing file -> defaults; bad file -> error."""
        if path is None:
            return cls().validated()
        path = Path(path)
        if not path.exists():
            return cls().validated()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FlakeHerdError(f"config {path}: invalid JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise FlakeHerdError(f"config {path}: top-level value must be an object")
        return cls.from_dict(raw).validated()

    @classmethod
    def from_dict(cls, raw: dict) -> Config:
        kwargs: dict = {}
        for key, value in raw.items():
            if key in _INT_FIELDS:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise FlakeHerdError(f"config: '{key}' must be an integer")
                kwargs[key] = value
            elif key in _BOOL_FIELDS:
                if not isinstance(value, bool):
                    raise FlakeHerdError(f"config: '{key}' must be a boolean")
                kwargs[key] = value
            elif key == "ignore":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise FlakeHerdError("config: 'ignore' must be a list of strings")
                kwargs[key] = value
            # Unknown keys are ignored on purpose: forward compatibility.
        return cls(**kwargs)


def with_overrides(base: Config, **changes) -> Config:
    """Return a copy of *base* with non-None values from *changes* applied."""
    changes = {k: v for k, v in changes.items() if v is not None}
    return replace(base, **changes).validated()


def effective_quarantine_min_score(cfg: Config, cli_min_score: int | None = None) -> int:
    """The minimum score a test needs to be quarantined under *cfg*.

    * Policy disabled: whatever ``quarantine_min_score`` says (legacy
      behavior, default 0 = every eligible class qualifies).
    * Policy enabled: the policy floor applies; ``quarantine_min_score``
      and an explicit ``--min-score`` can only *raise* the bar. The
      threshold is a safety floor, not a ceiling.
    """
    floor = cfg.quarantine_score_threshold if cfg.quarantine_threshold_enabled else 0
    return max(cfg.quarantine_min_score, floor, cli_min_score or 0)


def effective_quarantine_min_samples(cfg: Config) -> int:
    """Minimum recorded samples required for quarantine under *cfg*.

    Only enforced while the threshold policy is enabled (opt-in): the
    legacy behavior of ``flakeherd quarantine`` is preserved exactly for
    users who did not switch the policy on.
    """
    return cfg.quarantine_min_samples if cfg.quarantine_threshold_enabled else 0


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_QUARANTINE_MIN_SAMPLES",
    "DEFAULT_QUARANTINE_SCORE_THRESHOLD",
    "Config",
    "effective_quarantine_min_samples",
    "effective_quarantine_min_score",
    "with_overrides",
]

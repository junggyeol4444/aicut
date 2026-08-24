"""Exception types shared by the whole pipeline."""


class AicutError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(AicutError):
    """A calibration profile is missing a key or holds a value of the wrong shape."""


class UnmeasuredParameterError(ConfigError):
    """A provisional parameter was read in a context that forbids provisional values.

    17.5: a value that has not been measured must never be presented as final.
    Production runs may opt into ``strict`` mode, which turns every read of a
    provisional parameter into this error instead of a warning.
    """


class PipelineError(AicutError):
    """A pipeline stage could not produce its output."""


class RenderError(AicutError):
    """The renderer (ffmpeg) failed. The edit plan survives; only the render is lost (16장)."""


class PlanValidationError(AicutError):
    """An edit plan does not satisfy the edit-plan schema."""


class QuotaExceeded(AicutError):
    """YouTube Data API daily quota is spent. Carries the next PT-midnight reset (11.4)."""

    def __init__(self, message: str, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at


class ProviderError(AicutError):
    """The reasoning provider returned something unusable."""

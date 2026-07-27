"""Per-request telemetry for FastAPI services.

``RequestTelemetry`` tracks step timings and per-service API-call counters for a
single request. Consumers parameterize it with the API services they care about
(``api_call_keys``), the PostHog ``distinct_id``, and an ``event_prefix`` used
to namespace step events (e.g. ``"lookup"`` produces ``lookup_parse``,
``lookup_search``, ``lookup_completed``).

``send_to_posthog`` emits one PostHog event per tracked step plus a summary
event carrying total duration, step timings, API-call counts, and the cache
stats from :mod:`wxyc_fastapi.observability.cache_stats`.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wxyc_fastapi.observability.cache_stats import _BASE_KEYS, get_cache_stats

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

# Sourced from cache_stats so the two modules can't drift if the base set
# changes in a later phase. Kept as a module constant rather than rebuilt per
# call since it's the fallback for an uninitialized request.
_DEFAULT_CACHE_STATS: dict[str, float] = dict.fromkeys(_BASE_KEYS, 0)


def _fmt_dur(value: float) -> str:
    """Format a millisecond duration for a ``Server-Timing`` ``dur`` param.

    Two-decimal precision with trailing zeros stripped, so integer-valued
    durations stay clean (``8102.00`` → ``8102``, ``18.4567`` → ``18.46``). The
    ``.2f`` step guarantees a ``.`` in the string, which stops ``rstrip("0")``
    from eating integer-part zeros (``1000.00`` → ``1000``). Fixed-point (never
    scientific) so multi-second durations stay parseable as plain floats.
    """
    return f"{value:.2f}".rstrip("0").rstrip(".")


# Server-Timing `desc` values are RFC 7230 quoted-string content, embedded in a
# header the consumer serializes with `.encode("latin-1")` (Starlette's
# `MutableHeaders.__setitem__`). `desc` is therefore coerced to a *whitelist*
# of printable ASCII (0x20-0x7e) rather than a blacklist of known-bad bytes.
# The whitelist is the only set that is simultaneously (a) safe from
# header/response splitting and (b) guaranteed to encode without raising: a
# blacklist that misses any code point > U+00FF (an accented letter, CJK, an
# emoji, the U+2028/U+2029 line separators) would let it survive sanitization
# and then raise `UnicodeEncodeError` at the latin-1 encode boundary -- a 500,
# not a splitting bug, but still an availability hole a "safe to embed in a
# header" helper must not open. So every code point outside printable ASCII --
# all C0/C1 control characters, DEL, and every non-ASCII character -- is
# dropped.
#
# From the surviving printable-ASCII set, the header's own structural
# delimiters `,` (entry separator) and `;` (param separator) are dropped:
# downstream parsers are not obligated to be quoting-aware when splitting on
# them -- request-o-matic's `parse_server_timing` splits every entry on a bare
# `,` and every param on a bare `;` -- so leaving either in would split one leg
# into two or inject a bogus param, and escaping wouldn't help a parser that
# never unescapes. `=` is dropped too, as defense-in-depth: a parser that scans
# for `dur=` anywhere in an entry (rather than splitting-then-prefix-matching)
# could otherwise be fooled by a `dur=` living inside the desc text. Finally
# `"` and `\` are escaped rather than dropped, per RFC 7230 (`\"`, `\\`), since
# they remain legal quoted-string content once escaped -- preserving the text.
_DESC_NON_ASCII_RE = re.compile(r"[^\x20-\x7e]")
_DESC_STRUCTURAL_RE = re.compile(r"[,;=]")


def _sanitize_desc(desc: str) -> str:
    """Coerce free text to a Server-Timing ``desc="..."`` quoted-string body.

    Whitelist, not blacklist: keeps only printable ASCII (``0x20``-``0x7e``),
    then drops the header's structural delimiters ``,`` ``;`` ``=`` and escapes
    ``\\`` and ``"`` per the RFC 7230 ``quoted-string`` grammar. Backslash is
    escaped before quote so the backslash introduced to escape a quote is not
    itself re-escaped. See the module comment above ``_DESC_NON_ASCII_RE`` for
    why the printable-ASCII whitelist -- and specifically its latin-1
    encodability guarantee -- is load-bearing.
    """
    ascii_only = _DESC_NON_ASCII_RE.sub("", desc)
    structural_stripped = _DESC_STRUCTURAL_RE.sub("", ascii_only)
    return structural_stripped.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class StepResult:
    """Outcome of a single tracked step."""

    duration_ms: float
    success: bool = True
    error_type: str | None = None


@dataclass
class RequestTelemetry:
    """Tracks performance metrics for a single request.

    Args:
        api_call_keys: Names of the upstream services this consumer counts API
            calls for (e.g. ``["groq", "discogs", "slack"]`` for rom,
            ``["discogs"]`` for LML).
        distinct_id: PostHog ``distinct_id`` to attach to every emitted event.
            Conventionally ``"<service-name>-service"``.
        event_prefix: Prefix used to namespace events. ``"lookup"`` produces
            ``lookup_<step>`` per-step events and a ``lookup_completed`` summary.
    """

    api_call_keys: list[str]
    distinct_id: str
    event_prefix: str

    steps: dict[str, StepResult] = field(default_factory=dict)
    api_calls: dict[str, int] = field(init=False)
    start_time: float = field(default_factory=time.perf_counter)

    _current_step: str | None = field(default=None, repr=False)
    _step_start: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.api_calls = dict.fromkeys(self.api_call_keys, 0)

    @contextmanager
    def track_step(self, step_name: str):
        """Time a named step. Records ``StepResult`` even on exception (and re-raises)."""
        self._current_step = step_name
        self._step_start = time.perf_counter()
        error_type: str | None = None
        try:
            yield
        except Exception as e:
            error_type = type(e).__name__
            raise
        finally:
            duration_ms = (time.perf_counter() - self._step_start) * 1000
            self.steps[step_name] = StepResult(
                duration_ms=duration_ms,
                success=error_type is None,
                error_type=error_type,
            )
            self._current_step = None

    def record_api_call(self, service: str) -> None:
        """Increment the API-call counter for ``service``.

        Unknown services log a warning and are otherwise ignored, matching the
        existing rom/LML behavior.
        """
        if service in self.api_calls:
            self.api_calls[service] += 1
        else:
            logger.warning("Unknown service for API call tracking: %s", service)

    def get_total_duration_ms(self) -> float:
        """Elapsed wall-clock time since this telemetry was created, in milliseconds."""
        return (time.perf_counter() - self.start_time) * 1000

    def get_step_timings(self) -> dict[str, float]:
        """Step durations as ``{<name>_ms: <duration>}``."""
        return {f"{name}_ms": step.duration_ms for name, step in self.steps.items()}

    def as_server_timing(
        self,
        extra: dict[str, float] | None = None,
        descriptions: dict[str, str] | None = None,
    ) -> str:
        """Serialize tracked steps as a ``Server-Timing`` response-header value.

        Emits one ``<name>;dur=<ms>`` entry per tracked step (in insertion order),
        then one entry per ``extra`` item — used for durations derived outside
        :meth:`track_step`, e.g. LML's ``discogs`` (``pg_time_ms + api_time_ms``)
        or, on request-o-matic, the forwarded LML sub-stages — then exactly one
        live ``total;dur=`` from :meth:`get_total_duration_ms`. Entries are joined
        with ``", "`` per the `Server-Timing grammar
        <https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing>`_.

        ``total`` is sampled when this method is called, a few milliseconds before
        :meth:`send_to_posthog` samples its own ``total_duration_ms`` — the two are
        independent measurements, not identical. Durations are rounded to two
        decimals with trailing zeros stripped (``8102.00`` → ``8102``).

        The method always appends its own canonical ``total``; ``extra`` must not
        contain a ``total`` key (the request-o-matic merge drops the forwarded LML
        ``total`` before passing the remaining stages here) or the header will
        carry two.

        ``descriptions`` is an optional, strictly opt-in ``{leg_name: text}`` map
        — a leg is any tracked-step name, ``extra`` key, or ``"total"``. A leg
        named in ``descriptions`` renders as ``name;desc="<sanitized>";dur=<ms>``
        instead of the plain ``name;dur=<ms>`` form. Names with no entry — the
        default, and the entire map when ``descriptions`` is omitted — are
        unaffected, so existing callers that only pass ``extra`` keep producing
        byte-identical output. This is meant for legs that are derived or
        overlapping rather than additive — e.g. LML's ``discogs`` leg double-
        counts time already reflected in another span — so summing every ``dur``
        in the header is not a valid way to reconstruct the total. Unrecognized
        keys in ``descriptions`` (names that are not a step, an ``extra`` key, or
        ``"total"``) are silently ignored.

        Text is run through :func:`_sanitize_desc` before being embedded: it is
        coerced to printable ASCII (non-ASCII code points and C0/C1/DEL control
        characters are dropped, so the value always survives the consumer's
        latin-1 header encode), the header's structural delimiters ``,`` ``;``
        ``=`` are stripped (a downstream parser is not obligated to be
        quoting-aware when splitting on them), and ``"``/``\\`` are escaped per
        the RFC 7230 ``quoted-string`` grammar. If sanitizing reduces the text
        to nothing, the leg falls back to the plain ``name;dur=<ms>`` form rather
        than emitting an empty ``desc=""``.
        """
        descriptions = descriptions or {}

        def render(name: str, duration_ms: float) -> str:
            dur_param = f"dur={_fmt_dur(duration_ms)}"
            raw_desc = descriptions.get(name)
            if raw_desc:
                sanitized = _sanitize_desc(raw_desc)
                if sanitized:
                    return f'{name};desc="{sanitized}";{dur_param}'
            return f"{name};{dur_param}"

        entries = [render(name, step.duration_ms) for name, step in self.steps.items()]
        if extra:
            entries.extend(render(name, value) for name, value in extra.items())
        entries.append(render("total", self.get_total_duration_ms()))
        return ", ".join(entries)

    def send_to_posthog(
        self,
        posthog_client: Posthog,
        extra_properties: dict[str, Any] | None = None,
    ) -> None:
        """Emit per-step events and a summary event to PostHog.

        Cache stats are read from
        :func:`wxyc_fastapi.observability.cache_stats.get_cache_stats`. When
        unset for the current async context, a zeroed dict of the base keys is
        used so summary events have a stable shape downstream.
        """
        extra_properties = extra_properties or {}

        for step_name, step_result in self.steps.items():
            posthog_client.capture(
                distinct_id=self.distinct_id,
                event=f"{self.event_prefix}_{step_name}",
                properties={
                    "step": step_name,
                    "duration_ms": round(step_result.duration_ms, 2),
                    "success": step_result.success,
                    "error_type": step_result.error_type,
                },
            )

        cache_data = get_cache_stats()
        cache_props = cache_data.copy() if cache_data else _DEFAULT_CACHE_STATS.copy()

        posthog_client.capture(
            distinct_id=self.distinct_id,
            event=f"{self.event_prefix}_completed",
            properties={
                "total_duration_ms": round(self.get_total_duration_ms(), 2),
                "steps": self.get_step_timings(),
                "api_calls": self.api_calls.copy(),
                "cache": cache_props,
                **extra_properties,
            },
        )

        logger.debug(
            "Sent telemetry: %d steps, total %.1fms",
            len(self.steps),
            self.get_total_duration_ms(),
        )

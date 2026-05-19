from __future__ import annotations

import sys
import time


class ProgressReporter:
    def __init__(self, enabled: bool = True, stream = None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._isatty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._phase_name: str | None = None
        self._phase_total: int | None = None
        self._phase_current = 0
        self._phase_started_at = 0.0
        self._last_render_at = 0.0
        self._line_open = False

    def start_phase(self, name: str, total: int | None = None) -> None:
        if not self.enabled:
            return
        self._close_line()
        self._phase_name = name
        self._phase_total = total if total is not None and total >= 0 else None
        self._phase_current = 0
        self._phase_started_at = time.perf_counter()
        self._last_render_at = 0.0
        self._render(force=True)

    def advance(self, step: int = 1) -> None:
        if not self.enabled or self._phase_name is None:
            return
        self._phase_current += step
        self._render()

    def update(self, current: int, total: int | None = None) -> None:
        if not self.enabled or self._phase_name is None:
            return
        self._phase_current = max(current, 0)
        if total is not None and total >= 0:
            self._phase_total = total
        self._render()

    def complete_phase(self, message: str | None = None) -> None:
        if not self.enabled or self._phase_name is None:
            return
        if self._phase_total is not None:
            self._phase_current = self._phase_total
        self._render(force=True)
        elapsed = time.perf_counter() - self._phase_started_at
        label = message or self._phase_name
        self._write_line(f"{label} complete in {elapsed:.1f}s")
        self._phase_name = None
        self._phase_total = None
        self._phase_current = 0

    def info(self, message: str) -> None:
        if not self.enabled:
            return
        self._close_line()
        self._write_line(message)

    def _render(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last_render_at < 0.2:
            return
        self._last_render_at = now
        message = self._format_progress_message()
        if self._isatty:
            print(f"\r{message:<100}", end="", file=self.stream, flush=True)
            self._line_open = True
        else:
            self._write_line(message)

    def _format_progress_message(self) -> str:
        assert self._phase_name is not None
        elapsed = max(time.perf_counter() - self._phase_started_at, 0.001)
        if self._phase_total is None:
            return f"{self._phase_name}: {self._phase_current} items, elapsed {elapsed:.1f}s"

        if self._phase_total == 0:
            return f"{self._phase_name}: 0/0 elapsed {elapsed:.1f}s eta 00:00"

        total = self._phase_total
        current = min(max(self._phase_current, 0), total)
        ratio = current / total
        eta_seconds = None
        if current > 0 and current < total:
            eta_seconds = elapsed * (total - current) / current
        bar_width = 24
        filled = int(round(ratio * bar_width))
        bar = "#" * filled + "-" * (bar_width - filled)
        eta_text = self._format_eta(eta_seconds)
        return (
            f"{self._phase_name}: [{bar}] {current}/{total} "
            f"({ratio * 100:5.1f}%) elapsed {elapsed:.1f}s eta {eta_text}"
        )

    def _format_eta(self, eta_seconds: float | None) -> str:
        if eta_seconds is None:
            return "--:--"
        eta_seconds = max(int(round(eta_seconds)), 0)
        minutes, seconds = divmod(eta_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _close_line(self) -> None:
        if self._line_open and self._isatty:
            print(file=self.stream, flush=True)
            self._line_open = False

    def _write_line(self, message: str) -> None:
        print(message, file=self.stream, flush=True)
        self._line_open = False
